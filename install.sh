#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/root/5m-poly-bot"
PY_BIN="$APP_DIR/.venv/bin/python"
PIP_BIN="$APP_DIR/.venv/bin/pip"

read -r -p "POLY_PRIVATE_KEY: " POLY_PRIVATE_KEY
read -r -p "POLY_PROXY_WALLET: " POLY_PROXY_WALLET
read -r -p "POLYGON_RPC_URL: " POLYGON_RPC_URL
read -r -p "TELEGRAM_BOT_TOKEN: " TELEGRAM_BOT_TOKEN
read -r -p "TELEGRAM_CHAT_ID: " TELEGRAM_CHAT_ID
read -r -p "DASHBOARD_PASSWORD: " DASHBOARD_PASSWORD

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if [ ! -d .git ]; then
  echo "Run install.sh inside the cloned repository at $APP_DIR"
  exit 1
fi

python3 -m venv .venv
"$PIP_BIN" install --upgrade pip
"$PIP_BIN" install -r requirements.txt

cat > .env <<EOF
POLY_PRIVATE_KEY=$POLY_PRIVATE_KEY
POLY_PROXY_WALLET=$POLY_PROXY_WALLET
POLYGON_RPC_URL=$POLYGON_RPC_URL
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
DASHBOARD_PASSWORD=$DASHBOARD_PASSWORD
EOF

install -m 644 poly-bot-dashboard.service /etc/systemd/system/poly-bot-dashboard.service
install -m 644 poly-bot-live.service /etc/systemd/system/poly-bot-live.service
install -m 644 poly-bot-test.service /etc/systemd/system/poly-bot-test.service

systemctl daemon-reload
systemctl enable poly-bot-dashboard.service
systemctl restart poly-bot-dashboard.service

IP_ADDR="$(hostname -I | awk '{print $1}')"
echo "Dashboard URL: http://$IP_ADDR:3001"
echo "Bot services are installed but not started. Use the dashboard to start live or dry-run."
