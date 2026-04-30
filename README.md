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
| `POLYGON_RPC_URL` | Primary Polygon RPC URL |
| `DASHBOARD_PASSWORD` | Password for protected dashboard actions |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for session ROI alerts |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for alerts |

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
