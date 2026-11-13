#!/usr/bin/env bash
# Fast Dissem installer for Debian / Ubuntu (Proxmox LXC, VM, or bare metal).
#
# Run as root inside the container/VM:
#   bash -c "$(wget -qO- https://raw.githubusercontent.com/CaliMiller/FastDissem/main/deploy/install.sh)"
# or, from a clone:   sudo bash deploy/install.sh
#
# Options (environment variables):
#   REPO=https://github.com/CaliMiller/FastDissem.git   repository to clone (default)
#   BRANCH=main                                branch to use
#   APP_DIR=/opt/fast_dissem                   install location
#   PORT=8787                                  web UI port
#
# Re-running it is safe: it updates the code and keeps your settings.

set -euo pipefail

# Everything runs inside main(), which bash reads completely before running it.
# That keeps the install correct even when the git pull below replaces this
# very file while it is running (that's how updates work).
main() {
REPO="${REPO:-https://github.com/CaliMiller/FastDissem.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/fast_dissem}"
PORT="${PORT:-8787}"
SVC_USER="fastdissem"
export DEBIAN_FRONTEND=noninteractive

msg()  { printf '\e[1;36m==>\e[0m %s\n' "$*"; }
die()  { printf '\e[1;31mERROR:\e[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root."
command -v apt-get >/dev/null || die "This installer supports Debian/Ubuntu (apt) only."

msg "Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git ca-certificates >/dev/null

# If this script runs from inside a clone, use that clone's location.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/../fastdissem/__main__.py" ] && [ ! -d "$APP_DIR/.git" ]; then
  SRC="$(cd "$SELF_DIR/.." && pwd)"
  if [ "$SRC" != "$APP_DIR" ]; then
    msg "Copying $SRC to $APP_DIR"
    mkdir -p "$APP_DIR"
    cp -a "$SRC/." "$APP_DIR/"
  fi
fi

if [ -d "$APP_DIR/.git" ]; then
  msg "Updating code in $APP_DIR"
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH" 2>/dev/null \
    && git -C "$APP_DIR" checkout --quiet "$BRANCH" \
    && git -C "$APP_DIR" pull --quiet --ff-only origin "$BRANCH" \
    || msg "Could not pull (no remote or local changes) – keeping current code"
elif [ ! -f "$APP_DIR/fastdissem/__main__.py" ]; then
  [ -n "$REPO" ] || die "Set REPO=https://github.com/<owner>/<repo>.git"
  msg "Cloning $REPO"
  git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

msg "Creating service user and Python environment"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" waitress
mkdir -p "$APP_DIR/data"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR/data"
chmod 700 "$APP_DIR/data"

PASS_FILE=/root/fastdissem-web-password.txt
if [ ! -f "$APP_DIR/data/config.json" ]; then
  msg "Writing first-run settings (web UI on the network, with a password)"
  TZ_NAME="$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
  [ "$TZ_NAME" = "Etc/UTC" ] && TZ_NAME="America/Indiana/Indianapolis"
  WEB_PASS="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16 || true)"
  runuser -u "$SVC_USER" -- env PYTHONPATH="$APP_DIR" WEB_PASS="$WEB_PASS" TZ_NAME="$TZ_NAME" PORT="$PORT" \
    "$APP_DIR/.venv/bin/python" - "$APP_DIR/data/config.json" <<'PY'
import os, sys
from zoneinfo import ZoneInfo
from fastdissem.config import ConfigStore
c = ConfigStore(sys.argv[1]); d = c.raw()
d["web"].update(host="0.0.0.0", port=int(os.environ["PORT"]), password=os.environ["WEB_PASS"])
try:
    ZoneInfo(os.environ["TZ_NAME"]); d["general"]["timezone"] = os.environ["TZ_NAME"]
except Exception:
    pass
c.save(d)
PY
  echo "$WEB_PASS" > "$PASS_FILE"
  chmod 600 "$PASS_FILE"
fi

msg "Installing systemd service"
cat > /etc/systemd/system/fastdissem.service <<EOF
[Unit]
Description=Fast Dissem - YouTube & NWS alerts to social media
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m fastdissem run --data-dir $APP_DIR/data
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
# lets the web UI use ports below 1024, such as 80, without running as root
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/bin/fastdissem-update <<EOF
#!/usr/bin/env bash
# Pull the latest Fast Dissem from GitHub and restart. Settings are kept.
set -e
APP_DIR="$APP_DIR" BRANCH="\${BRANCH:-$BRANCH}" bash "$APP_DIR/deploy/install.sh"
EOF
chmod 755 /usr/local/bin/fastdissem-update

systemctl daemon-reload
systemctl enable --quiet fastdissem
systemctl restart fastdissem
sleep 3
systemctl is-active --quiet fastdissem || { journalctl -u fastdissem -n 30 --no-pager; die "Service failed to start."; }

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
WEB_PORT="$(runuser -u "$SVC_USER" -- env PYTHONPATH="$APP_DIR" "$APP_DIR/.venv/bin/python" -c \
  "import json;print(json.load(open('$APP_DIR/data/config.json'))['web']['port'])" 2>/dev/null || echo "$PORT")"
echo
msg "Fast Dissem is running."
echo "    Web UI:    http://${IP:-<this-machine>}:${WEB_PORT}"
if [ -f "$PASS_FILE" ]; then
  echo "    Password:  $(cat "$PASS_FILE")   (also saved in $PASS_FILE; change it in Settings → Web UI)"
fi
echo "    Logs:      journalctl -u fastdissem -f"
echo "    Update:    fastdissem-update"
echo "    It starts in DRY RUN mode – nothing is posted until you turn that off in Settings."
}

main "$@"
