#!/usr/bin/env bash
# Install salah-times: CLI symlink + two systemd user units (web :8381, notifier).
# Pure stdlib python — no virtualenv, no pip.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$UNIT_DIR" "$HOME/bin"
chmod +x "$REPO/cli.py" "$REPO/daemon.py" "$REPO/app/server.py"
ln -sf "$REPO/cli.py" "$HOME/bin/salah-times"
ln -sf "$REPO/systemd/salah-times.service" "$UNIT_DIR/salah-times.service"
ln -sf "$REPO/systemd/salah-times-notifier.service" "$UNIT_DIR/salah-times-notifier.service"

if ! loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -q yes; then
    loginctl enable-linger "$USER" 2>/dev/null || true
fi

systemctl --user daemon-reload
systemctl --user enable --now salah-times.service
systemctl --user enable --now salah-times-notifier.service

echo
echo "installed:"
echo "  dashboard : http://127.0.0.1:8381/"
echo "  units     : salah-times (web) · salah-times-notifier (daemon)"
echo "  CLI       : salah-times times | next | config | qibla"
echo "  config    : ${XDG_CONFIG_HOME:-$HOME/.config}/salah-times/config.json"
echo
sleep 1
systemctl --user --no-pager --lines=0 status salah-times.service salah-times-notifier.service || true
