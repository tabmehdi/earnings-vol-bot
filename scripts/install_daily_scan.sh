#!/usr/bin/env bash
# Sets up the daily scan on an Ubuntu VM: uv, project dependencies, a swap file and a
# systemd timer at [schedule].scan_start New York time on weekdays. The scan itself skips
# market holidays. Run from the cloned repository as a user with sudo rights:
#   bash scripts/install_daily_scan.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
SERVICE=evbot-scan

cd "$REPO_DIR"
if [[ ! -f .env ]]; then
  echo "Missing $REPO_DIR/.env with the Alpaca keys. Copy it to the VM first." >&2
  exit 1
fi
chmod 600 .env

echo "== uv"
if ! command -v uv >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
"$UV" --version

echo "== dependencies (Python 3.12 is downloaded by uv if missing)"
"$UV" sync --locked --no-dev

echo "== swap"
if [[ -z "$(swapon --show --noheadings)" ]]; then
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
else
  echo "swap already active:"
  swapon --show
fi

echo "== timer"
SCAN_START="$("$REPO_DIR/.venv/bin/python" -c \
  'from evbot.config import load_config; print(load_config().schedule.scan_start)')"
if [[ ! "$SCAN_START" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
  echo "[schedule].scan_start must be HH:MM, got '$SCAN_START'" >&2
  exit 1
fi
# The time zone is part of the calendar expression, so the VM's own zone does not matter.
ON_CALENDAR="Mon..Fri *-*-* ${SCAN_START}:00 America/New_York"
systemd-analyze calendar "$ON_CALENDAR"

sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<EOF
[Unit]
Description=evbot daily earnings scan (records signals, never trades)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/.venv/bin/python -m evbot.main_live scan
TimeoutStartSec=30min
EOF

sudo tee "/etc/systemd/system/${SERVICE}.timer" >/dev/null <<EOF
[Unit]
Description=Run the evbot scan at ${SCAN_START} New York time on weekdays

[Timer]
OnCalendar=${ON_CALENDAR}
AccuracySec=1s
# A scan started late is useless for the entry, so a missed run is not caught up.
Persistent=false
Unit=${SERVICE}.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE}.timer"
systemctl list-timers "${SERVICE}.timer" --no-pager
echo "Installed. Logs: ${REPO_DIR}/data/logs/scan_<date>.log and journalctl -u ${SERVICE}.service"
