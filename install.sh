#!/usr/bin/env bash
# Install the agent fleet on this host (run as root from a checkout of this repo).
# Safe to re-run: scripts and units are overwritten; agent folders (CLAUDE.md etc.) are only
# created when missing, because you and the agents edit them. Enabling is left to you, see README.md.
#
#   sudo ./install.sh [agents-source-dir]    default: examples/agents
set -euo pipefail
cd "$(dirname "$0")"
SRC=${1:-examples/agents}
CONF=/etc/claude-agents
AGENTS_DIR=/var/lib/claude-agents
# shellcheck source=/dev/null
[[ -f $CONF/fleet.conf ]] && source "$CONF/fleet.conf"

getent group agents >/dev/null || groupadd --system agents

install -m755 bin/claude-agent bin/agent-handoff /usr/local/bin/
install -m755 bin/agent-fix bin/agent-metrics /usr/local/bin/

install -d -m755 "$AGENTS_DIR" "$CONF" "$CONF/routines"
[[ -f $CONF/fleet.conf ]] || install -m644 -o root -g root config/fleet.conf "$CONF/fleet.conf"
install -m644 -o root -g root config/global.settings.json "$CONF/global.settings.json"
if compgen -G "examples/routines/*.txt" >/dev/null; then install -m644 examples/routines/*.txt "$CONF/routines/"; fi
install -d -m2770 -o root -g agents "$AGENTS_DIR/handoffs/delivered"
install -d -m3770 -o root -g agents "$AGENTS_DIR/handoffs/inbox"
# Event log written by claude-agent (task start/fix/resume/stop) as whichever user runs it; read by
# agent-metrics. Same approach as the inbox: group agents, setgid.
install -d -m2770 -o root -g agents /var/log/claude-agent
[[ -e /var/log/claude-agent/events.log ]] || install -m664 -o root -g agents /dev/null /var/log/claude-agent/events.log

for src in "$SRC"/*/; do
  name=$(basename "$src")
  user=$(sed -n 's/^RUN_AS=//p' "$src/agent.env"); user=${user:-root}
  dst=$AGENTS_DIR/$name
  if [[ $user != root ]]; then
    id "$user" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --comment "Claude agent ($name)" "$user"
    usermod -aG agents "$user"
    # Task sessions run in the agent user's own tmux server and need its /run/user/<uid>: keep it around.
    loginctl enable-linger "$user"
    # tasks resumed by root can run without the agents group; a user ACL keeps handoffs working
    setfacl -m "u:$user:rwx" "$AGENTS_DIR/handoffs/inbox" /var/log/claude-agent
    setfacl -m "u:$user:rw" /var/log/claude-agent/events.log
    # Non-root agents need Claude Code in their own home (then a one-time `claude` login as that user).
    if [[ ! -x $(getent passwd "$user" | cut -d: -f6)/.local/bin/claude ]]; then
      runuser -u "$user" -- bash -lc 'curl -fsSL https://claude.ai/install.sh | bash'
    fi
  fi
  # Config is sourced by root: keep it root-owned and outside the agent's writable folder.
  install -m644 -o root -g root "$src/agent.env" "$CONF/$name.env"
  # Deny rules / MCP approvals the agent must not be able to edit (passed with --settings).
  [[ -f $src/settings.json ]] && install -m644 -o root -g root "$src/settings.json" "$CONF/$name.settings.json"
  # Optional: base denies plus extras, for the master and normal task sessions (fix sessions use
  # settings.json); see the claude-agent header.
  [[ -f $src/task-settings.json ]] && install -m644 -o root -g root "$src/task-settings.json" "$CONF/$name.task-settings.json"
  if [[ ! -d $dst ]]; then
    cp -a "$src" "$dst"
    rm -f "$dst/agent.env" "$dst/settings.json" "$dst/task-settings.json"
    echo "created $dst"
  fi
  chown -R "$user:$user" "$dst"
  chmod 750 "$dst"
  if [[ -f $dst/secrets.env && ! -L $dst/secrets.env ]]; then chmod 600 "$dst/secrets.env"; fi
done

install -m644 systemd/*.service systemd/*.path systemd/*.timer /etc/systemd/system/
if compgen -G "examples/routines/*.timer" >/dev/null; then install -m644 examples/routines/*.timer /etc/systemd/system/; fi
systemctl daemon-reload
echo "installed; see README.md to enable agents and routines"

# Optional, recommended once agents run builds: cap each agent so one runaway task slows down
# instead of starving the host, e.g.
#   systemctl set-property claude-agent@<name>.service MemoryHigh=8G MemoryMax=10G CPUWeight=50 IOWeight=50
#   systemctl set-property user-<uid>.slice MemoryHigh=8G MemoryMax=10G   # that agent's task sessions
