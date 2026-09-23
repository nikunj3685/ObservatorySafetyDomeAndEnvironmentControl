#!/usr/bin/env bash
#
# Pi Safety Aggregator - one-shot installer
# ------------------------------------------
# Run this from inside the cloned repo, on the Raspberry Pi itself:
#
#   git clone https://github.com/nikunj3685/ObservatorySafetyDomeAndEnvironmentControl.git
#   cd ObservatorySafetyDomeAndEnvironmentControl
#   sudo bash install.sh
#
# What it does, in order:
#   1. Installs the system packages needed to build the Adafruit/GPIO
#      Python libraries (python3-pip, python3-dev, i2c-tools, git).
#   2. Enables I2C via raspi-config, if it isn't already (needed for the
#      BME280 and MLX90614 sensors).
#   3. pip-installs requirements.txt system-wide, matching the systemd
#      unit below which runs the service with plain `python3`, no venv.
#   4. Writes a dome-safety.service unit pointed at THIS directory and
#      the user who invoked sudo (not root), installs it, and starts it.
#   5. Prints the dashboard URL and the one-time `visudo` line needed for
#      the Settings page's Restart Service / Reboot Pi buttons - printed
#      only, never applied automatically, since editing sudoers
#      unattended isn't something this script should risk getting wrong.
#
# Safe to re-run any time (e.g. after `git pull`-ing an update) - every
# step below is idempotent and finishes with a service restart.
#
# See Observatory_Setup_Guide.docx for the hardware wiring (GPIO/I2C pin
# assignments) this script doesn't and can't handle for you.

set -euo pipefail

# ---------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------

if [ "$(id -u)" -ne 0 ]; then
    echo "This installer needs root (for apt, raspi-config, and systemd)." >&2
    echo "Re-run it as:  sudo bash install.sh" >&2
    exit 1
fi

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$INSTALL_DIR"

if [ ! -f "dome_safety_service.py" ]; then
    echo "dome_safety_service.py not found in $INSTALL_DIR" >&2
    echo "Run this script from inside the cloned repo." >&2
    exit 1
fi

# Real (non-root) user to run the service as and to own the working
# directory - whoever actually invoked sudo, so the systemd unit and its
# generated dome_config.json/logs/ai_training don't end up root-owned.
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    SERVICE_USER="$SUDO_USER"
else
    SERVICE_USER="$(id -un 1000 2>/dev/null || echo pi)"
    echo "Warning: couldn't determine who invoked sudo; defaulting the service" >&2
    echo "to run as '$SERVICE_USER'. Re-run as 'sudo bash install.sh' from a" >&2
    echo "normal login (not already root) if that's wrong." >&2
fi

IS_PI=false
if [ -f /proc/device-tree/model ] && grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
    IS_PI=true
fi
if [ "$IS_PI" = false ]; then
    echo "Warning: this doesn't look like a Raspberry Pi. Continuing anyway," >&2
    echo "but the GPIO/I2C sensor libraries below will likely fail to install" >&2
    echo "or to actually talk to hardware on anything else." >&2
fi

echo "Installing into: $INSTALL_DIR"
echo "Service will run as: $SERVICE_USER"
echo

# ---------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------

echo "==> Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3-pip python3-dev python3-venv i2c-tools git >/dev/null

# ---------------------------------------------------------------------
# 2. Enable I2C (BME280 + MLX90614 both need it)
# ---------------------------------------------------------------------

I2C_REBOOT_NEEDED=false
if command -v raspi-config >/dev/null 2>&1; then
    echo "==> Enabling I2C..."
    # do_i2c uses raspi-config's inverted convention: 0 = enable, 1 = disable.
    if raspi-config nonint get_i2c 2>/dev/null | grep -q "^1$"; then
        raspi-config nonint do_i2c 0
        I2C_REBOOT_NEEDED=true
    else
        echo "    already enabled."
    fi
else
    echo "==> raspi-config not found - skipping I2C enable step."
    echo "    Enable it yourself: sudo raspi-config -> Interface Options -> I2C."
fi

# ---------------------------------------------------------------------
# 3. Python dependencies
# ---------------------------------------------------------------------

echo "==> Installing Python packages from requirements.txt..."
# Raspberry Pi OS (Bookworm+) marks the system Python as externally
# managed (PEP 668); --break-system-packages matches the systemd unit
# below, which runs the service with the system python3 directly, no
# venv. Falls back to a plain install for older pip that doesn't know
# the flag.
if ! pip3 install --break-system-packages -r requirements.txt 2>/tmp/pip_err.log; then
    if grep -q "break-system-packages" /tmp/pip_err.log; then
        pip3 install -r requirements.txt
    else
        cat /tmp/pip_err.log >&2
        exit 1
    fi
fi
rm -f /tmp/pip_err.log

# ---------------------------------------------------------------------
# 4. systemd service
# ---------------------------------------------------------------------

echo "==> Installing the systemd service..."
SERVICE_PATH="/etc/systemd/system/dome-safety.service"
SYSTEMCTL_BIN="$(command -v systemctl || echo /usr/bin/systemctl)"

cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Pi Safety Aggregator - Dome/SafetyMonitor/ObservingConditions Alpaca server + web dashboard
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/dome_safety_service.py
Restart=on-failure
RestartSec=5

# Needed for the Settings page's Service Control "Restart service"/"Reboot
# Pi" buttons (dome_safety_service.py shells out to sudo systemctl for
# both) - see the visudo line this script prints at the end. Not needed
# for the service to run at all, only for those two buttons to work.
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOF

# So the service user (not root) owns whatever it creates here on first
# run - dome_config.json, status_history.json, logs/, ai_training/.
chown "$SERVICE_USER" "$INSTALL_DIR" 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now dome-safety.service

sleep 2
if systemctl is-active --quiet dome-safety.service; then
    echo "    dome-safety.service is active and running."
else
    echo "    dome-safety.service did not start - check:  systemctl status dome-safety.service" >&2
fi

# ---------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------

PI_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PI_IP="${PI_IP:-<pi-ip-address>}"

echo
echo "======================================================================"
echo " Done."
echo
echo " Dashboard:   http://$PI_IP:11112/"
echo " Logs:        journalctl -u dome-safety.service -f"
echo " Restart:     sudo systemctl restart dome-safety.service"
echo
if [ "$I2C_REBOOT_NEEDED" = true ]; then
    echo " I2C was just enabled for the first time - reboot before the BME280"
    echo " / MLX90614 sensors will actually respond:  sudo reboot"
    echo
fi
echo " Optional: to let the Settings page's Restart Service / Reboot Pi"
echo " buttons work without a password prompt, run 'sudo visudo' and add:"
echo
echo "   $SERVICE_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL_BIN restart dome-safety.service, $SYSTEMCTL_BIN reboot"
echo
echo " See Observatory_Setup_Guide.docx for GPIO/I2C wiring and every"
echo " Settings page in detail."
echo "======================================================================"
