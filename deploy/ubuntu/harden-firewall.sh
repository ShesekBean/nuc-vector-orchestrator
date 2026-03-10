#!/usr/bin/env bash
# harden-firewall.sh
#
# Host-level firewall hardening for OpenClaw + Docker.
# Run ONCE on the host machine (not inside Docker).
#
# What this does:
#   1. Enables ufw with deny-inbound / allow-outbound defaults
#   2. Allows SSH (so you don't lock yourself out)
#   3. Adds DOCKER-USER iptables rules — defense-in-depth against Docker's
#      iptables bypass, in case ports are ever changed from 127.0.0.1 to 0.0.0.0
#
# Note: if ports stay bound to 127.0.0.1 in docker-compose.signal.yml (the default),
# step 3 is extra protection only. The 127.0.0.1 binding alone is sufficient.
#
# Usage:
#   chmod +x harden-firewall.sh
#   sudo ./harden-firewall.sh
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
  echo "ERROR: must be run as root (run with elevated privileges)" >&2
  exit 1
fi

echo "==> Detecting public network interface..."
PUBLIC_IFACE=$(ip route | awk '/^default/ { print $5; exit }')
if [[ -z "$PUBLIC_IFACE" ]]; then
  echo "ERROR: could not detect default interface." >&2
  echo "  Override: PUBLIC_IFACE=eth0 ./harden-firewall.sh (as root)" >&2
  exit 1
fi
echo "    Interface: $PUBLIC_IFACE"

# ─── 1. UFW defaults ─────────────────────────────────────────────────────────
echo ""
echo "==> Setting ufw defaults..."
ufw default deny incoming
ufw default allow outgoing

# ─── 2. Allow SSH first (prevents lockout) ───────────────────────────────────
echo ""
echo "==> Allowing SSH..."
ufw allow ssh comment "SSH"

# ─── 3. DOCKER-USER rules (defense-in-depth) ─────────────────────────────────
#
# Docker bypasses ufw's INPUT chain by rewriting iptables directly.
# The DOCKER-USER chain is the correct place for user-defined rules that
# Docker WILL respect. These rules block external access to OpenClaw ports
# on the public interface even if port binding is accidentally changed.
#
# Implementation: insert into the EXISTING *filter block in after.rules.
# (Appending a new *filter block is wrong and breaks ufw — fixed here.)
UFW_AFTER_RULES="/etc/ufw/after.rules"
MARKER="DOCKER-USER openclaw"

echo ""
echo "==> Adding DOCKER-USER rules to $UFW_AFTER_RULES..."

if grep -q "$MARKER" "$UFW_AFTER_RULES"; then
  echo "    Already present — skipping."
else
  cp "$UFW_AFTER_RULES" "${UFW_AFTER_RULES}.bak.$(date +%Y%m%d%H%M%S)"

  # Insert the two DROP rules BEFORE the first COMMIT line in the *filter block.
  # Using Python for reliable multi-line insertion (sed -i varies by platform).
  python3 - "$UFW_AFTER_RULES" "$PUBLIC_IFACE" "$MARKER" <<'PY'
import sys, re

path, iface, marker = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    content = f.read()

rules = (
    f"# {marker}\n"
    f"-A DOCKER-USER -i {iface} -p tcp --dport 18789 -j DROP\n"
    f"-A DOCKER-USER -i {iface} -p tcp --dport 18790 -j DROP\n"
)

# Insert before the first COMMIT line (which is inside the *filter block)
updated = re.sub(r'^COMMIT$', rules + 'COMMIT', content, count=1, flags=re.MULTILINE)
if updated == content:
    print("ERROR: could not find COMMIT line in " + path, file=sys.stderr)
    sys.exit(1)

with open(path, 'w') as f:
    f.write(updated)
PY
  echo "    Rules inserted (backup: ${UFW_AFTER_RULES}.bak.*)"
fi

# ─── 4. Enable + reload ───────────────────────────────────────────────────────
echo ""
echo "==> Enabling ufw..."
ufw --force enable
ufw reload

# ─── 5. Verify ───────────────────────────────────────────────────────────────
echo ""
echo "==> ufw status:"
ufw status verbose

echo ""
echo "==> DOCKER-USER chain (active once Docker has started a container):"
iptables -L DOCKER-USER -n --line-numbers 2>/dev/null \
  || echo "    (not yet created — will appear after next 'docker compose up')"

echo ""
echo "Done."
echo ""
echo "Ports 18789 and 18790 are blocked on $PUBLIC_IFACE."
echo ""
echo "To allow LAN access (e.g. companion app from your phone):"
echo "  ufw allow from 192.168.1.0/24 to any port 18789 proto tcp  # run as root"
echo "  Then change 127.0.0.1 → 0.0.0.0 in docker-compose.signal.yml"
