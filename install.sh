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
  local default_value="${4:-}"
  local value=""
  while [ -z "$value" ]; do
    if [ "$secret" = "1" ]; then
      if [ -n "$default_value" ]; then
        read -r -s -p "$prompt_text [hidden, press Enter to keep current]: " value
        if [ -z "$value" ]; then
          value="$default_value"
        fi
      else
        read -r -s -p "$prompt_text: " value
      fi
      echo
    else
      if [ -n "$default_value" ]; then
        read -r -p "$prompt_text [$default_value]: " value
        if [ -z "$value" ]; then
          value="$default_value"
        fi
      else
        read -r -p "$prompt_text: " value
      fi
    fi
  done
  printf -v "$var_name" '%s' "$value"
}

read_env_value() {
  local key="$1"
  if [ ! -f "$ENV_FILE" ]; then
    return 0
  fi
  awk -F= -v search_key="$key" '$1 == search_key { sub(/^[^=]*=/, "", $0); print $0; exit }' "$ENV_FILE"
}

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run install.sh as root."
  exit 1
fi

require_cmd git
require_cmd python3
require_cmd systemctl
require_cmd install
require_cmd npm

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if [ ! -d .git ]; then
  echo "Run install.sh inside the cloned repository at $APP_DIR"
  exit 1
fi

CURRENT_POLY_PRIVATE_KEY="$(read_env_value POLY_PRIVATE_KEY)"
CURRENT_POLY_PROXY_WALLET="$(read_env_value POLY_PROXY_WALLET)"
CURRENT_POLYGON_RPC_URL="$(read_env_value POLYGON_RPC_URL)"
CURRENT_POLYGON_RPC_URLS="$(read_env_value POLYGON_RPC_URLS)"
CURRENT_TELEGRAM_BOT_TOKEN="$(read_env_value TELEGRAM_BOT_TOKEN)"
CURRENT_TELEGRAM_CHAT_ID="$(read_env_value TELEGRAM_CHAT_ID)"
CURRENT_DASHBOARD_PASSWORD="$(read_env_value DASHBOARD_PASSWORD)"
CURRENT_POLY_SIGNATURE_TYPE="$(read_env_value POLY_SIGNATURE_TYPE)"
CURRENT_CLOB_API_KEY="$(read_env_value CLOB_API_KEY)"
CURRENT_CLOB_SECRET="$(read_env_value CLOB_SECRET)"
CURRENT_CLOB_PASS_PHRASE="$(read_env_value CLOB_PASS_PHRASE)"
CURRENT_RELAYER_API_KEY="$(read_env_value RELAYER_API_KEY)"
CURRENT_RELAYER_API_KEY_ADDRESS="$(read_env_value RELAYER_API_KEY_ADDRESS)"

prompt_value POLY_PRIVATE_KEY "POLY_PRIVATE_KEY" 1 "$CURRENT_POLY_PRIVATE_KEY"
prompt_value POLY_PROXY_WALLET "POLY_PROXY_WALLET" 0 "$CURRENT_POLY_PROXY_WALLET"
prompt_value POLYGON_RPC_URL "POLYGON_RPC_URL" 0 "$CURRENT_POLYGON_RPC_URL"
prompt_value POLYGON_RPC_URLS "POLYGON_RPC_URLS (comma-separated)" 0 "${CURRENT_POLYGON_RPC_URLS:-$CURRENT_POLYGON_RPC_URL}"
prompt_value POLY_SIGNATURE_TYPE "POLY_SIGNATURE_TYPE" 0 "${CURRENT_POLY_SIGNATURE_TYPE:-2}"
prompt_value CLOB_API_KEY "CLOB_API_KEY" 1 "$CURRENT_CLOB_API_KEY"
prompt_value CLOB_SECRET "CLOB_SECRET" 1 "$CURRENT_CLOB_SECRET"
prompt_value CLOB_PASS_PHRASE "CLOB_PASS_PHRASE" 1 "$CURRENT_CLOB_PASS_PHRASE"
prompt_value RELAYER_API_KEY "RELAYER_API_KEY" 1 "$CURRENT_RELAYER_API_KEY"
prompt_value RELAYER_API_KEY_ADDRESS "RELAYER_API_KEY_ADDRESS" 0 "$CURRENT_RELAYER_API_KEY_ADDRESS"
prompt_value TELEGRAM_BOT_TOKEN "TELEGRAM_BOT_TOKEN" 1 "$CURRENT_TELEGRAM_BOT_TOKEN"
prompt_value TELEGRAM_CHAT_ID "TELEGRAM_CHAT_ID" 0 "$CURRENT_TELEGRAM_CHAT_ID"
prompt_value DASHBOARD_PASSWORD "DASHBOARD_PASSWORD" 1 "$CURRENT_DASHBOARD_PASSWORD"

python3 -m venv .venv
"$PIP_BIN" install --upgrade pip
"$PIP_BIN" install -r requirements.txt
npm install

cat > "$ENV_FILE" <<EOF
POLY_PRIVATE_KEY=$POLY_PRIVATE_KEY
POLY_PROXY_WALLET=$POLY_PROXY_WALLET
POLY_SIGNATURE_TYPE=$POLY_SIGNATURE_TYPE
CLOB_API_KEY=$CLOB_API_KEY
CLOB_SECRET=$CLOB_SECRET
CLOB_PASS_PHRASE=$CLOB_PASS_PHRASE
RELAYER_API_KEY=$RELAYER_API_KEY
RELAYER_API_KEY_ADDRESS=$RELAYER_API_KEY_ADDRESS
POLYGON_RPC_URL=$POLYGON_RPC_URL
POLYGON_RPC_URLS=$POLYGON_RPC_URLS
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
DASHBOARD_PASSWORD=$DASHBOARD_PASSWORD
EOF
chmod 600 "$ENV_FILE"

install -m 644 poly-bot-dashboard.service "$SYSTEMD_DIR/poly-bot-dashboard.service"
install -m 644 poly-bot-live.service "$SYSTEMD_DIR/poly-bot-live.service"
install -m 644 poly-bot-test.service "$SYSTEMD_DIR/poly-bot-test.service"
install -m 644 poly-bot-redeem.service "$SYSTEMD_DIR/poly-bot-redeem.service"
install -m 644 poly-bot-redeem.timer "$SYSTEMD_DIR/poly-bot-redeem.timer"

systemctl daemon-reload
systemctl enable poly-bot-dashboard.service
systemctl disable poly-bot-live.service >/dev/null 2>&1 || true
systemctl disable poly-bot-test.service >/dev/null 2>&1 || true
systemctl disable poly-bot-redeem.timer >/dev/null 2>&1 || true
systemctl stop poly-bot-live.service >/dev/null 2>&1 || true
systemctl stop poly-bot-test.service >/dev/null 2>&1 || true
systemctl stop poly-bot-redeem.service >/dev/null 2>&1 || true
systemctl stop poly-bot-redeem.timer >/dev/null 2>&1 || true
systemctl restart poly-bot-dashboard.service

IP_ADDR="$(hostname -I | awk '{print $1}')"
echo
echo "Installation complete."
echo "Dashboard URL: http://$IP_ADDR:3001"
echo "First open will show the setup wizard."
echo "Bot services are installed but not started. Use the dashboard to start live or dry-run."
echo "Auto-redeem service/timer are installed but disabled by default. Enable only after a successful dry-run test."
