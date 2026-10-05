#!/bin/sh
# Install or upgrade temperq on a Raspberry Pi with an AHT30 on I2C.
# Run from a checkout of this repo:  sudo deploy/install.sh
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/temperq
CONF_DIR=/etc/temperq

apt-get update
apt-get install -y python3-venv i2c-tools

# 32-bit Pi wheels come from piwheels; PyPI has few armv6 wheels. Bookworm configures
# piwheels globally, but early Trixie images didn't, so pass it explicitly.
PIP_EXTRA_INDEX_URL=https://www.piwheels.org/simple
# Trixie mounts /tmp as a RAM-backed tmpfs, too small on a Pi Zero if pip ever has to
# build a package from source.
TMPDIR=/var/tmp
export PIP_EXTRA_INDEX_URL TMPDIR

if ! id temperq >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin temperq
fi
usermod -a -G i2c temperq

# Turn on I2C (takes effect immediately; also persisted for the next boot).
raspi-config nonint do_i2c 0

mkdir -p "$APP_DIR" "$CONF_DIR"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install --upgrade "$REPO_DIR"

if [ ! -f "$CONF_DIR/config.yaml" ]; then
    install -m 0644 "$REPO_DIR/deploy/config.example.yaml" "$CONF_DIR/config.yaml"
fi
if [ ! -f "$CONF_DIR/credentials.yaml" ]; then
    install -m 0600 -o temperq -g temperq "$REPO_DIR/deploy/credentials.example.yaml" "$CONF_DIR/credentials.yaml"
fi

install -m 0644 "$REPO_DIR/deploy/temperq.service" /etc/systemd/system/temperq.service
systemctl daemon-reload

cat <<EOF

Installed. Next steps:
  1. Edit $CONF_DIR/config.yaml (MQTT host).
  2. Edit $CONF_DIR/credentials.yaml (MQTT username/password).
  3. Log in to ElectraSmart:  sudo $APP_DIR/.venv/bin/temperq login --credentials $CONF_DIR/credentials.yaml
  4. Check the sensor:         i2cdetect -y 1   (expect 38)
                               sudo -u temperq $APP_DIR/.venv/bin/temperq read-sensor --config $CONF_DIR/config.yaml
  5. Start it:                 sudo systemctl enable --now temperq && journalctl -u temperq -f
EOF
