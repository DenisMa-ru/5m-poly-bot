# 📈 Crypto Up/Down 5min Bot

A Python trading bot for **ETH and BTC Up/Down 5-minute markets on [Polymarket](https://polymarket.com)**, powered by real-time Binance price data.

The bot uses a **Window Delta strategy** combined with micro momentum and ATR volatility filtering to identify high-confidence entries in the final seconds before each market closes.

---

## ✨ Features

- **Window Delta filter** — compares current crypto price to the period's open price (fetched from Binance); skips entries when the price is too close to the line
- **Micro momentum confirmation** — checks the direction of the last 2 × 1-minute candles to reinforce the signal
- **ATR volatility filter** — skips entries when the current period's range exceeds 1.5× the historical average (too volatile)
- **Composite confidence score** — normalized 0–100% signal strength; configurable minimum threshold
- **Direction alignment check** — Binance trend must match the Polymarket leading side before entering
- **Per-crypto price thresholds** — stricter entry price for BTC (≥ 0.94) than ETH (≥ 0.92)
- **Three run modes** — Paper (simulated), Live (real funds), Dry Run (real data, no execution)
- **Parallel data fetching** — Polymarket and Binance queries run concurrently via `ThreadPoolExecutor`
- **Session summary** — prints a full trade log on exit (Ctrl+C)

---

## 🧠 Strategy

Every 5 minutes Polymarket resolves whether ETH (or BTC) closed **Up** or **Down** relative to its price at the start of the period.

The bot wakes up ~65 seconds before each market close and begins monitoring. It only places a bet when **all** of the following conditions are met:

| Condition | Description |
|---|---|
| **Entry window** | Between 10 and 50 seconds before close |
| **PM price** | Polymarket CLOB mid-price ≥ `PRICE_MIN` and ≤ 0.99 |
| **Window Delta** | `\|current − open\| / open` > 0.05% (not too close to the line) |
| **Confidence** | Composite score ≥ 30% (configurable) |
| **Direction match** | Binance delta direction == Polymarket leading side |
| **ATR** | Current period range ≤ 1.5× historical ATR |

### Score Weighting

| Delta magnitude | Weight |
|---|---|
| > 1.0% | 7 |
| > 0.20% | 5 |
| > 0.10% | 3 |
| > 0.05% | 1 |
| Momentum confirms | +3 |
| Higher timeframe trend confirms | +2 |
| **Max possible** | **12** |

Confidence = `abs(score) / 12.0`, capped at 100%.

---

## 📋 Requirements

- Python **3.10+**
- A [Polymarket](https://polymarket.com) account with USDC on Polygon (for live trading)
- No Binance account needed (public API)

---

## 🚀 Installation

Recommended server install flow:

```bash
git clone https://github.com/DenisMa-ru/5m-poly-bot.git /root/5m-poly-bot
cd /root/5m-poly-bot
chmod +x install.sh
sudo ./install.sh
```

What `install.sh` does:

- asks only for secrets and infrastructure values
- creates `.venv`
- installs dependencies from `requirements.txt`
- writes `.env`
- installs `systemd` units for dashboard, live bot, and dry-run bot
- starts only the dashboard
- leaves bot services stopped until you start them from the web UI

After installation:

- open the printed dashboard URL
- complete the first-run setup wizard
- start `dry-run` or `live` from the `Settings` tab

---

## ⚙️ Configuration

All key parameters are defined at the top of `crypto_bot.py`:

| Constant | Default | Description |
|---|---|---|
| `ENTRY_SECONDS_MIN` | `10` | Earliest entry (seconds before close) |
| `ENTRY_SECONDS_MAX` | `50` | Latest entry (seconds before close) |
| `PRICE_MIN["ETH"]` | `0.92` | Minimum Polymarket price for ETH entries |
| `PRICE_MIN["BTC"]` | `0.94` | Minimum Polymarket price for BTC entries |
| `PRICE_MAX` | `0.99` | Maximum Polymarket price (upside floor) |
| `DELTA_SKIP` | `0.0005` | Minimum delta (< 0.05% → skip) |
| `DELTA_WEAK` | `0.001` | Weak signal threshold (0.10%) |
| `DELTA_STRONG` | `0.002` | Strong signal threshold (0.20%) |
| `MIN_CONFIDENCE` | `0.3` | Minimum composite confidence (0.0–1.0) |
| `ATR_PERIODS` | `5` | Number of 5min candles for ATR calculation |
| `ATR_MULTIPLIER` | `1.5` | Maximum allowed range vs ATR |
| `WAKE_BEFORE` | `65` | Seconds before close to start monitoring |
| `POLL_INTERVAL` | `3` | Polling interval in seconds |

---

## 🖥️ Usage

```bash
# Paper mode — simulated trades, real data (default)
python crypto_bot.py --paper

# Dry run — real data, no trades, no keys needed
python crypto_bot.py --dry-run

# Live mode — real funds (requires .env keys)
python crypto_bot.py --live

# Live mode with custom bet size
python crypto_bot.py --live --amount 25
```

Press **Ctrl+C** at any time to stop the bot and print the session summary.

---

## 🔑 Environment Variables

Create a `.env` file based on `.env.example` if you are not using `install.sh`:

| Variable | Description |
|---|---|
| `POLY_PRIVATE_KEY` | Your Polymarket wallet private key (Polygon) |
| `POLY_PROXY_WALLET` | Your Polymarket proxy/funder wallet address |
| `POLY_SIGNATURE_TYPE` | Optional signature mode override: `0` = EOA, `1` = Magic/Polymarket proxy wallet, `2` = Gnosis Safe-style proxy wallet |
| `CLOB_API_KEY` | Optional pre-derived Polymarket CLOB API key for server/runtime use |
| `CLOB_SECRET` | Optional pre-derived Polymarket CLOB API secret for server/runtime use |
| `CLOB_PASS_PHRASE` | Optional pre-derived Polymarket CLOB API passphrase for server/runtime use |
| `POLYGON_RPC_URL` | Primary Polygon RPC URL |
| `DASHBOARD_PASSWORD` | Password for protected dashboard actions |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for session ROI alerts |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for alerts |

---

## Polymarket live auth notes

- If `CLOB_API_KEY`, `CLOB_SECRET`, and `CLOB_PASS_PHRASE` are absent, the bot may try to derive fresh CLOB credentials from Polymarket at runtime.
- On some servers this request can be blocked by Cloudflare on `/auth/api-key`, which breaks live execution even when wallet keys are correct.
- In that case, derive the CLOB credentials once from a non-blocked machine and save them into the server `.env`.
- For proxy-wallet setups, the correct `POLY_SIGNATURE_TYPE` depends on how Polymarket indexed the account. Do not assume `1` or `2` blindly.
- In one verified live case, `POLY_SIGNATURE_TYPE=1` allowed API auth but returned `balance=0` and `allowance=0`, while `POLY_SIGNATURE_TYPE=2` returned the real collateral balance and working allowance and allowed matched live buys.
- If live auth works but the bot logs `SKIP — insufficient collateral/allowance $0.00 < $1.00`, test `get_balance_allowance(...)` under both `POLY_SIGNATURE_TYPE=1` and `2` before changing strategy logic.

One-shot export command:

```bash
python crypto_bot.py --export-clob-creds
```

The command prints ready-to-paste `CLOB_API_KEY=...`, `CLOB_SECRET=...`, and `CLOB_PASS_PHRASE=...` lines for your `.env`.

Server note:

```bash
.venv/bin/python crypto_bot.py --export-clob-creds
```

Use `python3` instead of `python` on hosts where `python` is unavailable.

---

## Auto-redeem for winning positions

- Resolved winning positions do not automatically become spendable collateral for new buys until they are redeemed.
- If live logs show `SKIP - insufficient spendable ...` while Polymarket still shows redeemable winnings, the usual root cause is funds being parked in redeemable positions rather than a broken strategy loop.
- This repository includes a separate redeem worker so redeem stays outside the main trading loop:
  - `redeem_worker.mjs`
  - `poly-bot-redeem.service`
  - `poly-bot-redeem.timer`
- The worker currently supports standard non-NegRisk CTF redemption. NegRisk live redemption support is Unknown / not implemented in this first version.

Recommended rollout:

```bash
npm run redeem:dry-run
npm run redeem:one
sudo systemctl enable --now poly-bot-redeem.timer
```

Useful server checks:

```bash
sudo systemctl status poly-bot-redeem.timer
sudo systemctl status poly-bot-redeem.service
tail -n 100 /root/5m-poly-bot/redeem-worker.log
```

Required env for live redeem:

- `POLY_PRIVATE_KEY`
- `POLY_PROXY_WALLET`
- `POLY_SIGNATURE_TYPE`
- `RELAYER_API_KEY`
- `RELAYER_API_KEY_ADDRESS`
- `POLYGON_RPC_URL` is recommended

The live bot now logs spendable diagnostics more explicitly when funds are blocked, including `balance`, `allowance`, `onchain`, and the source used for the check. This helps distinguish true allowance issues from cash that is still locked in positions waiting for redeem.

---

## 📁 Project Structure

```
copy-trader/
├── crypto_bot.py             # Main bot
├── dashboard.py              # Streamlit dashboard
├── install.sh                # Server installation helper
├── poly-bot-live.service     # Live bot systemd unit
├── poly-bot-test.service     # Dry-run bot systemd unit
├── poly-bot-dashboard.service # Dashboard systemd unit
├── requirements.txt          # Python dependencies
├── .env.example              # Environment variable template
├── .gitignore                # Git ignore rules
└── README.md                 # This file
```

## Runtime model

- Actual runtime mode is detected from the active `systemd` bot service.
- Desired mode is configured in the dashboard and used by the `Start bot` action.
- Current session state is stored in `session_state.json`.
- Live all-time reset marker is stored in `stats_state.json`.

---

## ⚠️ Disclaimer

> **This software is for educational and experimental purposes only.**
>
> - This is **not financial advice**.
> - Prediction markets involve **real financial risk**. You can lose your entire investment.
> - Past performance of any strategy does not guarantee future results.
> - Use at your own risk. The authors accept no liability for any losses incurred.
> - Always start with `--paper` or `--dry-run` mode before using real funds.
> - Never risk money you cannot afford to lose.

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

## Collaborations

If this project was useful to you, contributions are welcome via USDC on Polygon:

**`0x6c0A4390033d15d2c15F1E6E03D59035A00188C1`**
