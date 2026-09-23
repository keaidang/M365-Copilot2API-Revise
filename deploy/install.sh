#!/usr/bin/env bash
# One-command installer for M365 Copilot2API + the bundled GPT-image-2
# drawing site (image-site/), as two systemd services behind one reverse proxy.
#
# Target: fresh Debian/Ubuntu server with systemd. Safe to re-run:
#   - binaries and static files are refreshed from this repository
#   - existing /etc/*.env files and databases are NEVER overwritten
#
# Usage:
#   sudo ./deploy/install.sh
#   DRY_RUN=1 ./deploy/install.sh     # print actions, change nothing
#
# Paths (override via environment): BIN_DST GW_DATA GW_ENV SITE_DIR SITE_DATA
#                                   SITE_ENV UNIT_DIR
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DST="${BIN_DST:-/usr/local/bin/m365-copilot2api}"
GW_DATA="${GW_DATA:-/var/lib/m365-copilot2api}"
GW_ENV="${GW_ENV:-/etc/m365-copilot2api.env}"
SITE_DIR="${SITE_DIR:-/opt/m365-image-site}"
SITE_DATA="${SITE_DATA:-/var/lib/m365-image-site}"
SITE_ENV="${SITE_ENV:-/etc/m365-image-site.env}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
DRY_RUN="${DRY_RUN:-0}"

log() { printf '[install] %s\n' "$*"; }
run() {
  if [ "$DRY_RUN" = 1 ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi
}
write_file() { # stdin -> $1
  if [ "$DRY_RUN" = 1 ]; then
    printf '[dry-run] write %s\n' "$1"
    cat > /dev/null
  else
    cat > "$1"
  fi
}

if [ "$DRY_RUN" != 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root (sudo $0)" >&2
  exit 1
fi
if [ ! -f "$REPO_DIR/image-site/server.py" ]; then
  echo "error: incomplete checkout (image-site/server.py not found under $REPO_DIR)" >&2
  exit 1
fi

# ------------------------------------------------------------------ gateway
if ! command -v go >/dev/null 2>&1; then
  echo "error: Go not found in PATH (need Go >= 1.23: https://go.dev/dl/)" >&2
  exit 1
fi
log "building gateway ($(go version | awk '{print $3}'))..."
BUILD_TMP="$(mktemp)"
trap 'rm -f "$BUILD_TMP"' EXIT
( cd "$REPO_DIR" && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o "$BUILD_TMP" ./cmd/server )
run install -D -m 0755 "$BUILD_TMP" "$BIN_DST"

run mkdir -p "$GW_DATA"
if [ -f "$GW_ENV" ]; then
  log "keeping existing $GW_ENV"
else
  log "creating $GW_ENV"
  write_file "$GW_ENV" <<EOF
# M365 gateway environment, read by systemd (created by deploy/install.sh).
# Loopback only - a reverse proxy fronts the gateway with public HTTPS.
M365_LISTEN=127.0.0.1:4141
M365_DATA_DIR=$GW_DATA
M365_LOG_LEVEL=info
# Web console administrator password - set before first start:
# M365_ADMIN_PASSWORD=
EOF
  if [ "$DRY_RUN" != 1 ]; then chmod 600 "$GW_ENV"; fi
fi

# -------------------------------------------------------------- image site
run mkdir -p "$SITE_DIR"
run install -m 0644 "$REPO_DIR/image-site/server.py" \
                    "$REPO_DIR/image-site/index.html" \
                    "$REPO_DIR/image-site/login.html" "$SITE_DIR/"
run mkdir -p "$SITE_DATA"
if ! id image-site >/dev/null 2>&1; then
  run useradd --system --home-dir "$SITE_DATA" --shell /usr/sbin/nologin image-site
fi
run chown -R image-site:image-site "$SITE_DATA"
if [ -f "$SITE_ENV" ]; then
  log "keeping existing $SITE_ENV"
else
  log "creating $SITE_ENV"
  SECRET="$(openssl rand -hex 32 2>/dev/null || { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; })"
  write_file "$SITE_ENV" <<EOF
# GPT-image-2 drawing site environment (created by deploy/install.sh).
# Mode 600: holds the server-side drawing API key and the session secret.
IMAGE_SITE_HOST=127.0.0.1
IMAGE_SITE_PORT=4180
IMAGE_SITE_UPSTREAM=http://127.0.0.1:4141
IMAGE_SITE_DB=$SITE_DATA/users.db
# Gateway API key with image permissions (web console -> API keys). Never
# exposed to the browser: it only lives here, server-side.
IMAGE_SITE_API_KEY=
# Session cookie signing secret, generated at install time (>= 32 bytes).
IMAGE_SITE_SESSION_SECRET=$SECRET
# One-time bootstrap admin for the drawing site itself; delete both after
# the first login (accounts are independent and PBKDF2-hashed).
IMAGE_SITE_INITIAL_USERNAME=
IMAGE_SITE_INITIAL_PASSWORD=
EOF
  if [ "$DRY_RUN" != 1 ]; then chmod 600 "$SITE_ENV"; fi
fi

# ------------------------------------------------------------------- units
run mkdir -p "$UNIT_DIR"
run install -m 0644 "$REPO_DIR/deploy/systemd/m365-copilot2api.service" "$UNIT_DIR/m365-copilot2api.service"
run install -m 0644 "$REPO_DIR/deploy/systemd/image-site.service" "$UNIT_DIR/image-site.service"
run systemctl daemon-reload
run systemctl enable m365-copilot2api.service image-site.service
run systemctl restart m365-copilot2api.service image-site.service

log "done."
cat <<EOF

Next steps:
  1. Gateway admin password: edit $GW_ENV (set M365_ADMIN_PASSWORD),
     then: systemctl restart m365-copilot2api
  2. Drawing site:           edit $SITE_ENV
       - IMAGE_SITE_API_KEY              (create a key in the gateway web console)
       - IMAGE_SITE_INITIAL_USERNAME/PASSWORD  (one-time first-login admin)
     then: systemctl restart image-site
  3. HTTPS reverse proxy:    adapt deploy/Caddyfile.example for your domain
  4. Open:                   https://<your-domain>/image/
EOF
