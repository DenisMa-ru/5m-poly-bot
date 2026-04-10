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
| Momentum confirms | +2 |
| **Max possible** | **9** |

Confidence = `abs(score) / 9.0`, capped at 100%.

---

## 📋 Requirements

- Python **3.10+**
- A [Polymarket](https://polymarket.com) account with USDC on Polygon (for live trading)
- No Binance account needed (public API)

---

## 🚀 Installation

```bash
# 1. Clone the repo
git clone https://github.com/your-username/copy-trader.git
cd copy-trader

# 2. Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up your environment variables
cp .env.example .env
# Edit .env and fill in your keys (only required for --live mode)
```

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

Only required for `--live` mode. Create a `.env` file based on `.env.example`:

| Variable | Description |
|---|---|
| `POLY_PRIVATE_KEY` | Your Polymarket wallet private key (Polygon) |
| `POLY_PROXY_WALLET` | Your Polymarket proxy/funder wallet address |

---

## 📁 Project Structure

```
copy-trader/
├── crypto_bot.py       # Main bot
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── .gitignore          # Git ignore rules
└── README.md           # This file
```

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
