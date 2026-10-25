#!/usr/bin/env bash
# One-line installer for the realm relay agent.
#
# Usage on each relay/exit machine:
#   curl -fsSL https://YOUR_PANEL_HOST/agent-install | TOKEN=xxxx PORT=5001 bash -
#
# Or fetch the script from the repo and run locally:
#   TOKEN=xxxx PORT=5001 bash install.sh
#
# What it does:
#   1. Downloads agent.py + realm-agent.service from this repo
#   2. Installs agent.py to /usr/local/bin/realm-agent (executable)
#   3. Writes the systemd unit with AGENT_TOKEN/AGENT_PORT baked in
#   4. Enables + starts the service
#   5. Prints the agent URL the panel should use
#
# Requires: bash, curl, systemd, root. Realm must already be installed
# and running as the `realm` systemd service (or adjust REALM_SERVICE).
set -euo pipefail

TOKEN="${TOKEN:-}"
PORT="${PORT:-5001}"
REALM_CONFIG="${REALM_CONFIG:-/etc/realm/config.json}"
REALM_SERVICE="${REALM_SERVICE:-realm}"
INSTALL_DIR="/usr/local/bin"
UNIT_DIR="/etc/systemd/system"
REPO_BASE="${REPO_BASE:-https://raw.githubusercontent.com/aoomee/realm-panel/main/realm-agent}"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  red "Must run as root. Try: sudo TOKEN=xxxx bash install.sh"
  exit 1
fi
if [[ -z "$TOKEN" ]]; then
  red "TOKEN env var is required. Example: TOKEN=my-secret bash install.sh"
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  red "curl is required. Install it first: apt-get install -y curl  /  yum install -y curl"
  exit 1
fi

# Auto-generate a strong token if user passed GENERATE_TOKEN=1
if [[ "$TOKEN" == "generate" || "${GENERATE_TOKEN:-0}" == "1" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    TOKEN="$(openssl rand -hex 24)"
  else
    TOKEN="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
  fi
  yellow "Generated token: $TOKEN"
fi

echo
yellow "==> Installing realm-agent to ${INSTALL_DIR}/realm-agent"
curl -fsSL "${REPO_BASE}/agent.py" -o "${INSTALL_DIR}/realm-agent"
chmod +x "${INSTALL_DIR}/realm-agent"

echo
yellow "==> Writing systemd unit to ${UNIT_DIR}/realm-agent.service"
# Pull the template, then substitute env lines so token isn't exposed via `systemctl show`
# of the upstream unit file. We write env into a separate EnvironmentFile the panel does not see.
ENV_FILE="/etc/realm/agent.env"
mkdir -p /etc/realm
cat > "$ENV_FILE" <<EOF
AGENT_TOKEN=${TOKEN}
AGENT_PORT=${PORT}
REALM_CONFIG=${REALM_CONFIG}
REALM_SERVICE=${REALM_SERVICE}
EOF
chmod 600 "$ENV_FILE"

# Pull the unit template and patch it to load the env file
TMP_UNIT="$(mktemp)"
curl -fsSL "${REPO_BASE}/realm-agent.service" -o "$TMP_UNIT"
# Make sure EnvironmentFile is active (uncomment if commented)
sed -i -E 's|^#?\s*EnvironmentFile=.*|EnvironmentFile='"${ENV_FILE}"'|' "$TMP_UNIT"
# Remove the inline AGENT_TOKEN/AGENT_PORT lines (they would override the env file)
sed -i -E '\|^Environment="AGENT_TOKEN=|d' "$TMP_UNIT"
sed -i -E '\|^Environment="AGENT_PORT=|d' "$TMP_UNIT"
sed -i -E '\|^Environment="REALM_CONFIG=|d' "$TMP_UNIT"
sed -i -E '\|^Environment="REALM_SERVICE=|d' "$TMP_UNIT"
install -m 0644 "$TMP_UNIT" "${UNIT_DIR}/realm-agent.service"
rm -f "$TMP_UNIT"

echo
yellow "==> Enabling + starting service"
systemctl daemon-reload
systemctl enable --now realm-agent
sleep 1

if systemctl is-active --quiet realm-agent; then
  green "✓ realm-agent is running"
else
  red "✗ realm-agent failed to start. Recent logs:"
  journalctl -u realm-agent --no-pager -n 20 || true
  exit 1
fi

# Detect the machine's primary public IP for the panel URL hint
PUB_IP="$(curl -fsS --max-time 4 https://api.ipify.org 2>/dev/null || true)"
if [[ -z "$PUB_IP" ]]; then
  PUB_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi

cat <<EOF

$(green '✓ Agent installed.')

  Agent URL:   http://${PUB_IP:-<this-machine-ip>}:${PORT}
  Token:       ${TOKEN}
  Config path: ${REALM_CONFIG}

Now in the realm panel (machine A), open 节点管理 → 添加节点 and paste:
  URL:   http://${PUB_IP:-<this-machine-ip>}:${PORT}
  Token: ${TOKEN}

Token is stored locally at ${ENV_FILE} (chmod 600). Keep it safe — anyone
with it can push new realm config to this machine.

EOF
