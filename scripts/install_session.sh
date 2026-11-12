#!/usr/bin/env bash
# Sets up the daily paper-trading session on the Ubuntu VM: uv, dependencies, a swap file and
# a systemd timer at [schedule].session_start New York time on weekdays. The session itself
# skips market holidays and reads each day's open and close (early closes included).
# It replaces the scan-only timer from scripts/install_daily_scan.sh: the session runs the scan.
# Run from the cloned repository as a user with sudo rights:
#   bash scripts/install_session.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
SERVICE=evbot-session
OLD_SCAN=evbot-scan

cd "$REPO_DIR"
if [[ ! -f .env ]]; then
  echo "Missing $REPO_DIR/.env with the Alpaca keys and ALPACA_PAPER=true." >&2
  exit 1
fi
chmod 600 .env

echo "== uv"
if ! command -v uv >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
"$UV" --version

echo "== dependencies"
"$UV" sync --locked --no-dev

echo "== swap"
if [[ -z "$(swapon --show --noheadings)" ]]; then
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
else
  swapon --show
fi

echo "== retire the scan-only timer (the session runs the scan now)"
if systemctl list-unit-files "${OLD_SCAN}.timer" --no-legend | grep -q "${OLD_SCAN}"; then
  sudo systemctl disable --now "${OLD_SCAN}.timer"
  sudo rm -f "/etc/systemd/system/${OLD_SCAN}.timer" "/etc/systemd/system/${OLD_SCAN}.service"
fi

echo "== timer"
SESSION_START="$("$REPO_DIR/.venv/bin/python" -c \
  'from evbot.config import load_config; print(load_config().schedule.session_start)')"
if [[ ! "$SESSION_START" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
  echo "[schedule].session_start must be HH:MM, got '$SESSION_START'" >&2
  exit 1
fi
ON_CALENDAR="Mon..Fri *-*-* ${SESSION_START}:00 America/New_York"
systemd-analyze calendar "$ON_CALENDAR"

sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<EOF
[Unit]
Description=evbot trading session (paper): exits, scan and entries
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/.venv/bin/python -m evbot.main_live session
# A crash restarts the session, which reconciles with the broker and resumes the day.
Restart=on-failure
RestartSec=60
# Exit code 2 is a configuration problem (e.g. not a paper account): do not retry it.
RestartPreventExitStatus=2
TimeoutStopSec=120
EOF

sudo tee "/etc/systemd/system/${SERVICE}.timer" >/dev/null <<EOF
[Unit]
Description=Start the evbot session at ${SESSION_START} New York time on weekdays

[Timer]
OnCalendar=${ON_CALENDAR}
AccuracySec=1s
# Missed while the VM was off: start on boot. The session picks up wherever the day is.
Persistent=true
Unit=${SERVICE}.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE}.timer"
systemctl list-timers "${SERVICE}.timer" --no-pager
echo "Installed. Logs: ${REPO_DIR}/data/logs/session_<date>.log and journalctl -u ${SERVICE}.service"
