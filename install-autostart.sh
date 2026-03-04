#!/bin/bash
# Install Snappi systemd service and passwordless sudo for user hamilton.
# Run from Snappi dir: ./install-autostart.sh   (or: bash install-autostart.sh)
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/snappi.service"
SUDOERS_FILE="$SCRIPT_DIR/snappi-sudoers"
if [[ ! -f "$SERVICE_FILE" ]]; then
  echo "Missing $SERVICE_FILE"
  exit 1
fi
# Passwordless sudo for hamilton (Snappi + PicoClaw can run sudo without a password)
if [[ -f "$SUDOERS_FILE" ]]; then
  sudo cp "$SUDOERS_FILE" /etc/sudoers.d/snappi-sudoers
  sudo chmod 440 /etc/sudoers.d/snappi-sudoers
  echo "Installed passwordless sudo for hamilton (/etc/sudoers.d/snappi-sudoers)"
else
  echo "Optional: add snappi-sudoers for passwordless sudo (see snappi-sudoers in repo)"
fi
sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable snappi
echo "Snappi service enabled. Start now with: sudo systemctl start snappi"
echo "Status: sudo systemctl status snappi   |   Logs: journalctl -u snappi -f"
