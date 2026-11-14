#!/bin/sh
# Installs the server on the Radxa as a service that starts at boot and restarts when it dies.
# Run it from a checkout of the repository, as root. Running it again updates the installed copy.
#
#   sudo sh deploy/install.sh
#
# What it does:
#   - installs the packages (python venv, libgpiod, i2c tools, bluez)
#   - copies the server to /opt/LowcostSlamServer and makes its virtual environment
#     (with the system packages, python3-libgpiod is not on PyPI as a wheel)
#   - installs the udev rules: names of the header I2C buses, gpio / PM QoS access, USB stick automount
#   - installs the systemd units (the API and the Bluetooth GATT server), enables them and (re)starts them
#   - keeps the journal on disk, so the log of a field problem survives a reboot
# Your /opt/LowcostSlamServer/.env (see deploy/env.example) is never overwritten.
#
# For tests the paths can be changed (PREFIX, UNIT_DIR, UDEV_DIR, ETC_DIR) and NO_SYSTEM=1 skips
# everything that needs root or the running system.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(dirname "$HERE")
PREFIX=${PREFIX:-/opt/LowcostSlamServer}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
UDEV_DIR=${UDEV_DIR:-/etc/udev/rules.d}
ETC_DIR=${ETC_DIR:-/etc}
NO_SYSTEM=${NO_SYSTEM:-0}
USER_NAME=${SUDO_USER:-radxa}
SERVICE=lowcost-slam-api
GATT_SERVICE=lowcost-slam-gatt

say() { printf '== %s\n' "$*"; }

if [ "$NO_SYSTEM" != 1 ] && [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo:  sudo sh deploy/install.sh" >&2
    exit 1
fi
[ -f "$SRC/app/main.py" ] || { echo "run this from a checkout of the repository ($SRC has no app/main.py)" >&2; exit 1; }

if [ "$NO_SYSTEM" != 1 ]; then
    say "packages"
    apt-get update -qq
    apt-get install -y -qq python3-venv python3-libgpiod python3-smbus2 i2c-tools bluez
    grep -qx i2c-dev "$ETC_DIR/modules-load.d/i2c-dev.conf" 2>/dev/null \
        || { mkdir -p "$ETC_DIR/modules-load.d"; echo i2c-dev >> "$ETC_DIR/modules-load.d/i2c-dev.conf"; }
    modprobe i2c-dev || true
    getent group gpio >/dev/null || groupadd --system gpio
    usermod -aG gpio,i2c "$USER_NAME" || true   # so $USER_NAME can run the sensors by hand too
fi

say "copying the server to $PREFIX"
mkdir -p "$PREFIX"
rm -rf "$PREFIX/app"
cp -a "$SRC/app" "$PREFIX/app"
cp -a "$SRC/requirements.txt" "$PREFIX/requirements.txt"
rm -rf "$PREFIX/gatt" "$PREFIX/shared" "$PREFIX/docs"
cp -a "$SRC/gatt" "$PREFIX/gatt"
cp -a "$SRC/shared" "$PREFIX/shared"
cp -a "$SRC/docs" "$PREFIX/docs"
find "$PREFIX/app" "$PREFIX/gatt" "$PREFIX/shared" -name __pycache__ -type d -prune -exec rm -rf {} +
if [ -d "$SRC/sdk" ]; then
    rm -rf "$PREFIX/sdk"
    cp -aL "$SRC/sdk" "$PREFIX/sdk"
else
    echo "WARNING: no sdk folder in $SRC: the camera SDK wrapper is missing (see README), the camera will not open" >&2
fi
[ -f "$PREFIX/.env" ] || cp "$HERE/env.example" "$PREFIX/.env.example"

say "virtual environment"
[ -x "$PREFIX/.venv/bin/python" ] || python3 -m venv --system-site-packages "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install -q --upgrade pip
"$PREFIX/.venv/bin/pip" install -q -r "$PREFIX/requirements.txt"

say "udev rules"
mkdir -p "$UDEV_DIR"
cp "$HERE/60-rockpix-header.rules" "$HERE/99-usb-automount.rules" "$UDEV_DIR/"

say "service"
mkdir -p "$UNIT_DIR"
cp "$HERE/$SERVICE.service" "$HERE/$GATT_SERVICE.service" "$UNIT_DIR/"
mkdir -p "$ETC_DIR/systemd/journald.conf.d"
cat > "$ETC_DIR/systemd/journald.conf.d/lowcost-slam.conf" <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=200M
EOF

if [ "$NO_SYSTEM" = 1 ]; then
    say "NO_SYSTEM=1: files are in place, nothing was started"
    exit 0
fi

udevadm control --reload
udevadm trigger --action=add --subsystem-match=gpio --subsystem-match=i2c-dev --subsystem-match=misc
udevadm settle
systemctl restart systemd-journald
systemctl daemon-reload
systemctl enable bluetooth
systemctl restart bluetooth
systemctl enable "$SERVICE" "$GATT_SERVICE"
systemctl restart "$SERVICE"
systemctl restart "$GATT_SERVICE"

say "done"
sleep 3
systemctl --no-pager --lines=15 status "$SERVICE" "$GATT_SERVICE" || true
cat <<EOF

The server and the Bluetooth service start at every boot. Useful:
  systemctl status $SERVICE $GATT_SERVICE      journalctl -u $SERVICE -u $GATT_SERVICE -f
  sudo systemctl restart $SERVICE              sudo systemctl disable --now $SERVICE $GATT_SERVICE
The phone finds the device by its Bluetooth name (SLAM-xxx) and the service UUID, see docs/GATT.md.
Settings: $PREFIX/.env (copy $PREFIX/.env.example). Log out and in once so '$USER_NAME' gets the gpio and i2c groups.
EOF
