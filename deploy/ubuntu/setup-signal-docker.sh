#!/usr/bin/env bash
# setup-signal-docker.sh
#
# One-shot setup: builds the openclaw:signal Docker image (with signal-cli)
# and starts the gateway with the security-hardened compose overlay.
#
# Usage:
#   1. cp .env.signal.template .env   # fill in all REPLACE_ME values first
#   2. chmod +x setup-signal-docker.sh
#   3. ./setup-signal-docker.sh
#
# Re-run to rebuild after a signal-cli version upgrade.
#
# ─── Firewall / UFW hardening (run once, manually) ───────────────────────────
#
# CRITICAL: Docker rewrites iptables rules and bypasses ufw by default.
# Binding ports to 127.0.0.1 in docker-compose.signal.yml is the primary
# mitigation. These ufw steps add defense-in-depth at the host level:
#
#   # 1. Enable ufw if not already active
#   sudo ufw enable
#
#   # 2. Default deny inbound, allow outbound
#   sudo ufw default deny incoming
#   sudo ufw default allow outgoing
#
#   # 3. Allow SSH (do this BEFORE enabling ufw or you'll lock yourself out)
#   sudo ufw allow ssh
#
#   # 4. Block direct access to Docker-managed ports from the outside.
#   #    Docker uses the DOCKER chain in iptables. To stop Docker from
#   #    punching holes past ufw, add this to /etc/ufw/after.rules BEFORE
#   #    the COMMIT line:
#   #
#   #      *filter
#   #      :DOCKER-USER - [0:0]
#   #      -A DOCKER-USER -i eth0 -p tcp --dport 18789 -j DROP
#   #      -A DOCKER-USER -i eth0 -p tcp --dport 18790 -j DROP
#   #      COMMIT
#   #
#   #    (Replace eth0 with your public interface: `ip route | grep default`)
#   #    Then reload: sudo ufw reload
#   #
#   #    OR use the simpler approach: keep ports bound to 127.0.0.1 in compose
#   #    (already done in docker-compose.signal.yml) which prevents Docker
#   #    from exposing them on the public interface at all.
#
#   # 5. If you need LAN access to the gateway (companion apps etc.):
#   #    sudo ufw allow from <your-LAN-CIDR> to any port 18789 proto tcp
#   #    e.g.: sudo ufw allow from 192.168.1.0/24 to any port 18789 proto tcp
#   #    AND change 127.0.0.1 → 0.0.0.0 in docker-compose.signal.yml ports
#
#   # 6. Verify:
#   sudo ufw status verbose
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
BASE_COMPOSE="$ROOT_DIR/docker-compose.yml"
SIGNAL_COMPOSE="$ROOT_DIR/docker-compose.signal.yml"
COMPOSE_ARGS=(-f "$BASE_COMPOSE" -f "$SIGNAL_COMPOSE")

# ─── Preflight ───────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed or not on PATH." >&2; exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: docker compose plugin not available (try: docker compose version)." >&2; exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: .env not found. Copy the template and fill it in first:"
  echo "  cp .env.signal.template .env"
  echo "  then edit .env and replace all REPLACE_ME values."
  exit 1
fi

if grep -v "^#" "$ENV_FILE" | grep -q "REPLACE_ME"; then
  echo "ERROR: .env still contains placeholder values (REPLACE_ME)."
  echo "  Edit .env and fill in every value before continuing."
  exit 1
fi

# ─── Resolve signal-cli version ──────────────────────────────────────────────
echo "==> Fetching latest signal-cli version..."
SIGNAL_CLI_VERSION=$(
  curl -fsSL https://api.github.com/repos/AsamK/signal-cli/releases/latest \
    | grep '"tag_name"' \
    | sed 's/.*"v\([^"]*\)".*/\1/'
) || {
  echo "ERROR: Could not fetch signal-cli version from GitHub." >&2
  echo "  Check your internet connection or set SIGNAL_CLI_VERSION manually." >&2
  exit 1
}
echo "    signal-cli version: $SIGNAL_CLI_VERSION"

# ─── Build base openclaw image first ─────────────────────────────────────────
echo ""
echo "==> Building openclaw:local (base image)..."
docker build \
  -t openclaw:local \
  -f "$ROOT_DIR/Dockerfile" \
  "$ROOT_DIR"

# ─── Build openclaw:signal image ─────────────────────────────────────────────
echo ""
echo "==> Building openclaw:signal (with signal-cli $SIGNAL_CLI_VERSION)..."
docker build \
  --build-arg BASE_IMAGE=openclaw:local \
  --build-arg SIGNAL_CLI_VERSION="$SIGNAL_CLI_VERSION" \
  -f "$ROOT_DIR/Dockerfile.signal" \
  -t openclaw:signal \
  "$ROOT_DIR"

# ─── Prepare directories ─────────────────────────────────────────────────────
# Source .env to expand ~ and get path values
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a
OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR/#\~/$HOME}"
OPENCLAW_WORKSPACE_DIR="${OPENCLAW_WORKSPACE_DIR/#\~/$HOME}"

echo ""
echo "==> Creating config directories..."
mkdir -p "$OPENCLAW_CONFIG_DIR" "$OPENCLAW_WORKSPACE_DIR" "$OPENCLAW_CONFIG_DIR/identity"

# ─── Onboard (first run only) ────────────────────────────────────────────────
OPENCLAW_JSON="$OPENCLAW_CONFIG_DIR/openclaw.json"
if [[ ! -f "$OPENCLAW_JSON" ]]; then
  echo ""
  echo "==> First-time onboarding..."
  echo "    When prompted:"
  echo "      - Gateway bind: lan"
  echo "      - Gateway auth: token"
  echo "      - Gateway token: $OPENCLAW_GATEWAY_TOKEN"
  echo "      - Tailscale exposure: Off"
  echo "      - Install Gateway daemon: No"
  echo ""
  docker compose "${COMPOSE_ARGS[@]}" --env-file "$ENV_FILE" \
    run --rm openclaw-cli onboard --no-install-daemon
else
  echo ""
  echo "    Config already exists at $OPENCLAW_JSON — skipping onboarding."
fi

# ─── Signal channel registration ─────────────────────────────────────────────
echo ""
echo "==> Signal setup"
echo ""
echo "You need a DEDICATED phone number for the bot (not your personal Signal number)."
echo ""
echo "--- Path A: Link an existing Signal app (QR) ---"
echo "  docker compose ${COMPOSE_ARGS[*]} --env-file .env \\"
echo "    run --rm openclaw-gateway signal-cli link -n 'OpenClaw'"
echo "  Scan the QR code in your Signal app (Linked Devices)."
echo ""
echo "--- Path B: Register a new dedicated number (SMS) ---"
echo "  BOT_NUMBER=+1XXXXXXXXXX"
echo ""
echo "  # Register (may require captcha — see https://github.com/AsamK/signal-cli/wiki/Registration-with-captcha)"
echo "  docker compose ${COMPOSE_ARGS[*]} --env-file .env \\"
echo "    run --rm openclaw-gateway signal-cli -a \"\$BOT_NUMBER\" register"
echo ""
echo "  # If captcha required:"
echo "  docker compose ${COMPOSE_ARGS[*]} --env-file .env \\"
echo "    run --rm openclaw-gateway \\"
echo "    signal-cli -a \"\$BOT_NUMBER\" register --captcha 'signalcaptcha://PASTE_URL_HERE'"
echo ""
echo "  # Verify with the SMS code:"
echo "  docker compose ${COMPOSE_ARGS[*]} --env-file .env \\"
echo "    run --rm openclaw-gateway signal-cli -a \"\$BOT_NUMBER\" verify CODE"
echo ""
echo "--- Configure OpenClaw ---"
echo "  After setup, edit $OPENCLAW_JSON (or use signal-config.json.example as a guide)"
echo "  and add your Signal channel config (see signal-config.json.example)."
echo ""

# ─── Start gateway ───────────────────────────────────────────────────────────
echo "==> Starting gateway..."
docker compose "${COMPOSE_ARGS[@]}" --env-file "$ENV_FILE" up -d openclaw-gateway

echo ""
echo "==> Gateway started."
echo ""
echo "  Ports:    127.0.0.1:${OPENCLAW_GATEWAY_PORT:-18789} (gateway), 127.0.0.1:${OPENCLAW_BRIDGE_PORT:-18790} (bridge)"
echo "  Config:   $OPENCLAW_CONFIG_DIR"
echo "  Token:    $OPENCLAW_GATEWAY_TOKEN"
echo ""
echo "  Logs:     docker compose -f docker-compose.yml -f docker-compose.signal.yml logs -f openclaw-gateway"
echo "  Status:   docker compose -f docker-compose.yml -f docker-compose.signal.yml exec openclaw-gateway node openclaw.mjs status"
echo ""
echo "After configuring Signal, approve your first DM pairing:"
echo "  docker compose -f docker-compose.yml -f docker-compose.signal.yml exec openclaw-gateway node openclaw.mjs pairing list signal"
echo "  docker compose -f docker-compose.yml -f docker-compose.signal.yml exec openclaw-gateway node openclaw.mjs pairing approve signal <CODE>"
