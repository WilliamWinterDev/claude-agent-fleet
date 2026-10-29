#!/usr/bin/env bash
# Install the optional claude-bridge add-on (run as root from a checkout of this repo, after the
# fleet's own install.sh). Safe to re-run: code, units and sudoers are overwritten; the three files
# in /etc/claude-bridge are only created when missing. Nothing is enabled or exposed: see
# docs/bridge.md for configuring and starting it.
#
#   sudo bridge/install.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")"
DRY_RUN=0
[[ ${1:-} == --dry-run ]] && DRY_RUN=1
CONF=/etc/claude-bridge

run() {
  if [[ $DRY_RUN == 1 ]]; then printf 'would run:'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

id claude-bridge >/dev/null 2>&1 ||
  run useradd --system --no-create-home --home-dir /var/lib/claude-bridge --shell /usr/sbin/nologin claude-bridge

run install -d -m755 /usr/local/lib/claude-bridge "$CONF"
for f in bridge_core.py bridge_server.py bridge_events.py webhook.py; do
  run install -m644 -o root -g root "lib/$f" /usr/local/lib/claude-bridge/
done
run install -m755 -o root -g root claude-bridge-helper /usr/local/sbin/claude-bridge-helper

[[ -e $CONF/bridge.env ]] || run install -m644 -o root -g root bridge.env.example "$CONF/bridge.env"
# Root-owned allow-list: the bridge user must never be able to change which sessions it reaches.
[[ -e $CONF/helper.conf ]] || run install -m644 -o root -g root helper.conf.example "$CONF/helper.conf"
if [[ ! -e $CONF/secrets.env ]]; then
  if [[ $DRY_RUN == 1 ]]; then
    echo "would create $CONF/secrets.env (0640 root:claude-bridge) with a random BRIDGE_BEARER_TOKEN"
  else
    ( umask 027
      { echo "BRIDGE_BEARER_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
        echo "BRIDGE_WEBHOOK_URL=REPLACE_ME"
        echo "BRIDGE_WEBHOOK_KEY=REPLACE_ME"; } > "$CONF/secrets.env" )
    chown root:claude-bridge "$CONF/secrets.env"
    chmod 640 "$CONF/secrets.env"
    echo "created $CONF/secrets.env with a new bearer token (not printed)"
  fi
fi

# sudoers: validate before installing so a typo can never break sudo
if [[ $DRY_RUN == 1 ]]; then
  echo "would run: visudo -cf claude-bridge.sudoers && install -m440 -> /etc/sudoers.d/claude-bridge"
else
  visudo -cf claude-bridge.sudoers
  install -m440 -o root -g root claude-bridge.sudoers /etc/sudoers.d/claude-bridge
fi

run install -m644 systemd/claude-bridge.slice systemd/claude-bridge.service /etc/systemd/system/
run install -m644 -o root -g root logrotate /etc/logrotate.d/claude-bridge
run systemctl daemon-reload
echo "installed; edit $CONF/helper.conf and $CONF/bridge.env, then: systemctl enable --now claude-bridge (see docs/bridge.md)"
