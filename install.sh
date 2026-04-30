#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/root/5m-poly-bot"
ENV_FILE="$APP_DIR/.env"
PIP_BIN="$APP_DIR/.venv/bin/pip"
SYSTEMD_DIR="/etc/systemd/system"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

prompt_value() {
  local var_name="$1"
  local prompt_text="$2"
  local secret="${3:-0}"
  local value=""
  while [ -z "$value" ]; do
    if [ "$secret" = "1" ]; then
      read -r -s -p "$prompt_text: " value
      echo
    else
      read -r -p "$prompt_text: " value
    fi
  done
  printf -v "$var_name" '%s' "$value"
}

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run install.sh as root."
  exit 1
fi

require_cmd git
require_cmd python3
require_cmd systemctl
require_cmd install

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if [ ! -d .git ]; then
  echo "Run install.sh inside the cloned repository at $APP_DIR"
  exit 1
fi

prompt_value POLY_PRIVATE_KEY "POLY_PRIVATE_KEY" 1
prompt_value POLY_PROXY_WALLET "POLY_PROXY_WALLET"
prompt_value POLYGON_RPC_URL "POLYGON_RPC_URL"
prompt_value TELEGRAM_BOT_TOKEN "TELEGRAM_BOT_TOKEN" 1
prompt_value TELEGRAM_CHAT_ID "TELEGRAM_CHAT_ID"
prompt_value DASHBOARD_PASSWORD "DASHBOARD_PASSWORD" 1

python3 -m venv .venv
"$PIP_BIN" install --upgrade pip
"$PIP_BIN" install -r requirements.txt

cat > "$ENV_FILE" <<EOF
POLY_PRIVATE_KEY=$POLY_PRIVATE_KEY
POLY_PROXY_WALLET=$POLY_PROXY_WALLET
POLYGON_RPC_URL=$POLYGON_RPC_URL
POLYGON_RPC_URLS=$POLYGON_RPC_URL
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
DASHBOARD_PASSWORD=$DASHBOARD_PASSWORD
EOF
chmod 600 "$ENV_FILE"

install -m 644 poly-bot-dashboard.service "$SYSTEMD_DIR/poly-bot-dashboard.service"
install -m 644 poly-bot-live.service "$SYSTEMD_DIR/poly-bot-live.service"
install -m 644 poly-bot-test.service "$SYSTEMD_DIR/poly-bot-test.service"

systemctl daemon-reload
systemctl enable poly-bot-dashboard.service
systemctl disable poly-bot-live.service >/dev/null 2>&1 || true
systemctl disable poly-bot-test.service >/dev/null 2>&1 || true
systemctl stop poly-bot-live.service >/dev/null 2>&1 || true
systemctl stop poly-bot-test.service >/dev/null 2>&1 || true
systemctl restart poly-bot-dashboard.service

IP_ADDR="$(hostname -I | awk '{print $1}')"
echo
echo "Installation complete."
echo "Dashboard URL: http://$IP_ADDR:3001"
echo "First open will show the setup wizard."
echo "Bot services are installed but not started. Use the dashboard to start live or dry-run."
