#!/usr/bin/env bash
# Install the brother-label systemd units and (optionally) the CUPS fallback
# hardening. Idempotent; safe to re-run. Requires sudo for the systemd steps.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system

echo "==> Symlinking CLI onto ~/.local/bin"
mkdir -p "$HOME/.local/bin"
ln -sf "$REPO/bin/label" "$HOME/.local/bin/label"
ln -sf "$REPO/bin/lazy-brother" "$HOME/.local/bin/lazy-brother"

echo "==> Installing systemd units from $REPO/systemd"
for unit in brother-keepalive.service brother-keepalive.timer \
            brother-watchdog.service brother-watchdog.timer; do
    sudo ln -sf "$REPO/systemd/$unit" "$UNIT_DIR/$unit"
done

echo "==> Reloading systemd and enabling timers"
sudo systemctl daemon-reload
sudo systemctl enable --now brother-keepalive.timer
sudo systemctl enable --now brother-watchdog.timer

# CUPS fallback hardening: keep the queue from auto-disabling on transient
# unreachability. Only matters when LABEL_USE_CUPS=1, but harmless to set.
if lpstat -p brother >/dev/null 2>&1; then
    echo "==> Setting CUPS error policy on 'brother' to retry-job"
    sudo lpadmin -p brother -o printer-error-policy=retry-job || true
fi

echo "==> Done. Timers:"
systemctl list-timers 'brother-*' --no-pager || true
