#!/usr/bin/env bash
# Fast Dissem – Proxmox VE helper: creates a Debian LXC and installs Fast Dissem in it.
#
# Run on the Proxmox HOST shell (Datacenter → your node → Shell), as root:
#   bash -c "$(wget -qO- https://raw.githubusercontent.com/<you>/<repo>/main/deploy/proxmox-lxc.sh)"
#
# Every prompt has a default; press Enter to accept. For an unattended install,
# set any of these and add -y:   CTID CT_HOSTNAME CORES RAM_MB DISK_GB BRIDGE IP GATEWAY
#                                ROOTFS_STORAGE TEMPLATE_STORAGE VLAN REPO BRANCH
#   e.g.  IP=192.168.1.50/24 GATEWAY=192.168.1.1 bash proxmox-lxc.sh -y

set -euo pipefail

# >>> After uploading to GitHub, put your repository here (or pass REPO=...) <<<
DEFAULT_REPO="https://github.com/CaliMiller/FastDissem.git"

REPO="${REPO:-$DEFAULT_REPO}"
BRANCH="${BRANCH:-main}"
YES=0; [ "${1:-}" = "-y" ] && YES=1

msg()  { printf '\e[1;36m==>\e[0m %s\n' "$*"; }
warn() { printf '\e[1;33m!!\e[0m %s\n' "$*"; }
die()  { printf '\e[1;31mERROR:\e[0m %s\n' "$*" >&2; exit 1; }
ask() {  # ask VAR "Question" default
  local var="$1" q="$2" def="$3" ans
  if [ -n "${!var:-}" ]; then return; fi
  if [ "$YES" -eq 1 ]; then printf -v "$var" '%s' "$def"; return; fi
  read -r -p "$q [$def]: " ans </dev/tty || true
  printf -v "$var" '%s' "${ans:-$def}"
}

[ "$(id -u)" -eq 0 ] || die "Run as root on the Proxmox host."
command -v pct >/dev/null && command -v pveam >/dev/null || die "This must run on a Proxmox VE host (pct not found)."
case "$REPO" in *CHANGE-ME*) die "Set your GitHub repo: edit DEFAULT_REPO in this script, or run with REPO=https://github.com/<you>/<repo>.git";; esac
if git ls-remote "$REPO" >/dev/null 2>&1 || ! command -v git >/dev/null; then :; else
  die "Can't reach $REPO. Is the repository public and the URL right?"
fi

echo
echo "  Fast Dissem – Proxmox LXC installer"
echo "  Repository: $REPO ($BRANCH)"
echo

# ---------------------------------------------------------------- choices
ask CTID      "Container ID"            "$(pvesh get /cluster/nextid)"
ask CT_HOSTNAME  "Hostname"                "fastdissem"
ask CORES     "CPU cores"               "1"
ask RAM_MB    "Memory (MB)"             "512"
ask DISK_GB   "Disk size (GB)"          "4"
ask BRIDGE    "Network bridge"          "vmbr0"
ask VLAN      "VLAN tag (blank = none)" ""
ask IP        "IPv4 (dhcp or CIDR like 192.168.1.50/24)" "dhcp"
if [ "$IP" != "dhcp" ]; then ask GATEWAY "Gateway" ""; fi

pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists."

DEF_ROOTFS="$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1}' | grep -m1 -x 'local-lvm' \
  || pvesm status -content rootdir | awk 'NR>1 && $3=="active"{print $1; exit}')"
DEF_TMPL="$(pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 && $3=="active"{print $1}' | grep -m1 -x 'local' \
  || pvesm status -content vztmpl | awk 'NR>1 && $3=="active"{print $1; exit}')"
ask ROOTFS_STORAGE   "Storage for the container disk" "${DEF_ROOTFS:-local-lvm}"
ask TEMPLATE_STORAGE "Storage for the OS template"    "${DEF_TMPL:-local}"

if [ "$YES" -eq 0 ]; then
  read -r -p "Create container $CTID ($CT_HOSTNAME, ${CORES} CPU, ${RAM_MB} MB, ${DISK_GB} GB on $ROOTFS_STORAGE, $IP)? [Y/n]: " ok </dev/tty || true
  case "${ok:-y}" in [Yy]*) ;; *) die "Cancelled.";; esac
fi

# ---------------------------------------------------------------- network
NET="name=eth0,bridge=${BRIDGE},ip=${IP}"
[ -n "${GATEWAY:-}" ] && NET+=",gw=${GATEWAY}"
[ -n "${VLAN:-}" ] && NET+=",tag=${VLAN}"

# -------------------------------------------------------- template + create
# Try the newest Debian first. Older Proxmox releases reject newer Debian
# templates ("unsupported debian version"), so fall back to the next one.
# Force a version with DEBIAN_VERSION=12 (or 13).
msg "Updating the template list"
pveam update >/dev/null 2>&1 || warn "pveam update failed; using the cached template list"
AVAILABLE="$(pveam available --section system 2>/dev/null | awk '{print $2}')"

CREATED=0
for v in ${DEBIAN_VERSION:-13 12}; do
  TEMPLATE="$(grep -E "^debian-${v}-standard_.*_amd64" <<<"$AVAILABLE" | sort -V | tail -1 || true)"
  if [ -z "$TEMPLATE" ]; then
    warn "No Debian $v template offered by this Proxmox host"
    continue
  fi
  if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
    msg "Downloading $TEMPLATE"
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null || { warn "Download failed"; continue; }
  fi
  msg "Creating container $CTID from $TEMPLATE"
  if OUT="$(pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
      --hostname "$CT_HOSTNAME" --cores "$CORES" --memory "$RAM_MB" --swap 512 \
      --rootfs "${ROOTFS_STORAGE}:${DISK_GB}" --net0 "$NET" \
      --unprivileged 1 --features nesting=1 --onboot 1 --timezone host \
      --tags fastdissem --description "Fast Dissem – ${REPO}" 2>&1)"; then
    CREATED=1
    break
  fi
  if grep -qi "unsupported debian version" <<<"$OUT"; then
    warn "This Proxmox version doesn't support Debian $v yet; trying an older Debian"
    pct status "$CTID" >/dev/null 2>&1 && pct destroy "$CTID" --purge >/dev/null 2>&1 || true
    continue
  fi
  echo "$OUT" >&2
  die "pct create failed."
done
[ "$CREATED" -eq 1 ] || die "Could not create the container with any Debian template. Try updating Proxmox (apt update && apt full-upgrade)."

msg "Starting container"
pct start "$CTID"

msg "Waiting for network"
for i in $(seq 1 60); do
  if pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1; then break; fi
  [ "$i" -eq 60 ] && die "Container has no internet access after 60 s. Check bridge/IP/gateway/DNS."
  sleep 1
done

# ----------------------------------------------------------------- install
msg "Installing Fast Dissem inside the container (takes a minute)"
pct exec "$CTID" -- bash -c "
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null
  rm -rf /opt/fast_dissem
  git clone --quiet --branch '$BRANCH' '$REPO' /opt/fast_dissem
  REPO='$REPO' BRANCH='$BRANCH' bash /opt/fast_dissem/deploy/install.sh
"

IPADDR="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
PASS="$(pct exec "$CTID" -- cat /root/fastdissem-web-password.txt 2>/dev/null || true)"

echo
msg "Done! Container $CTID ($CT_HOSTNAME)"
echo "    Web UI:   http://${IPADDR}:8787"
[ -n "$PASS" ] && echo "    Password: $PASS"
echo "    Update:   pct exec $CTID -- fastdissem-update"
echo "    Logs:     pct exec $CTID -- journalctl -u fastdissem -f"
echo "    Console:  pct enter $CTID"
echo "    It starts in DRY RUN mode – nothing is posted until you turn that off in Settings."
