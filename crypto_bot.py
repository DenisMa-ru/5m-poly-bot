"""
crypto_bot.py — ETH/BTC Up/Down 5min Trading Bot with Window Delta
Strategy:
  - Calculates Window Delta (current ETH price vs period-open price) from Binance
  - Only enters if the delta is large enough (near-certain outcome)
  - Enters between 10–50 seconds before close when Polymarket price >= 0.92

Improvements over previous version:
  - Binance Window Delta as primary filter (avoids entering near the line)
  - Micro momentum (direction of last 2 1min candles)
  - Composite score → configurable minimum confidence
  - Dry run mode (real data, no trades executed)

Usage:
    python crypto_bot.py --paper
    python crypto_bot.py --live
    python crypto_bot.py --dry-run      # real data, no trades
    python crypto_bot.py --live --amount 10
"""

import time
import json
import argparse
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import os
import signal
import sys

load_dotenv()

# ─── FILE PATHS ────────────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).parent
CONTROL_FILE = BOT_DIR / "control.json"
SIGNALS_FILE = BOT_DIR / "signals.json"
SETTINGS_FILE = BOT_DIR / "settings.json"
PID_FILE = BOT_DIR / "bot.pid"


def atomic_write_text(path: Path, content: str):
    """Atomically write text content to a file."""
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def load_signals_file() -> list:
    """Load signals history safely, falling back to an empty list."""
    if not SIGNALS_FILE.exists():
        return []
    try:
        data = json.loads(SIGNALS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_signals_file(signals: list):
    """Persist signals history atomically."""
    atomic_write_text(SIGNALS_FILE, json.dumps(signals[-10000:], indent=2))


def ensure_single_instance():
    """Prevent multiple bot processes from running at the same time."""
    if not PID_FILE.exists():
        atomic_write_text(PID_FILE, str(os.getpid()))
        return

    try:
        existing_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        atomic_write_text(PID_FILE, str(os.getpid()))
        return

    if existing_pid == os.getpid():
        return

    try:
        os.kill(existing_pid, 0)
    except OSError:
        atomic_write_text(PID_FILE, str(os.getpid()))
        return

    raise RuntimeError(f"Another bot process is already running with PID {existing_pid}")

# ─── SETTINGS ──────────────────────────────────────────────────────────────────
def load_settings():
    """Load settings from settings.json, fallback to defaults."""
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}

def load_bot_settings():
    """Загружает настройки из settings.json для CONFIG, fallback на дефолты."""
    try:
        s = json.loads(SETTINGS_FILE.read_text())
        return s
    except Exception:
        return {}

_bot_settings = load_bot_settings()


def get_enabled_coins(settings: dict) -> list[str]:
    """Return validated enabled coins from settings."""
    coins = settings.get("enabled_coins", ["BTC", "ETH"])
    if not isinstance(coins, list):
        return ["BTC", "ETH"]
    valid = [coin for coin in coins if coin in BINANCE_SYMBOLS]
    return valid or ["BTC", "ETH"]

def get_setting(key, default):
    """Get a setting from file or return default."""
    s = load_settings()
    return s.get(key, default)

# ─── SIGNAL SAVING ─────────────────────────────────────────────────────────────
def save_signal(signal_data):
    """Append a signal to signals.json for dashboard history."""
    try:
        signals = load_signals_file()
        signals.append(signal_data)
        save_signals_file(signals)
    except Exception as e:
        log(f"[SIGNAL SAVE ERROR] {e}")

# ─── CONTROL FILE CHECK ────────────────────────────────────────────────────────
def check_control():
    """Check control.json for stop/restart commands. Returns command or None."""
    try:
        if CONTROL_FILE.exists():
            data = json.loads(CONTROL_FILE.read_text())
            return data.get("cmd")
    except Exception:
        pass
    return None

# ─── CONFIG (defaults, can be overridden by settings.json) ────────────────────
GAMMA_API         = "https://gamma-api.polymarket.com"
CLOB_API          = "https://clob.polymarket.com"
BINANCE_API       = "https://api.binance.com"

ENTRY_SECONDS_MAX = _bot_settings.get("entry_max", 20)
ENTRY_SECONDS_MIN = _bot_settings.get("entry_min", 15)
PRICE_MIN         = {
    "BTC": _bot_settings.get("price_min_btc", 0.94),
    "ETH": _bot_settings.get("price_min_eth", 0.92),
}
PRICE_MAX         = _bot_settings.get("price_max", 0.99)

WAKE_BEFORE       = 65
POLL_INTERVAL     = 3

DELTA_SKIP        = _bot_settings.get("delta_skip", 0.0005)
DELTA_WEAK        = 0.001
DELTA_STRONG      = 0.002

MIN_CONFIDENCE    = _bot_settings.get("min_confidence", 0.3)

ATR_PERIODS       = 5
ATR_MULTIPLIER    = _bot_settings.get("atr_multiplier", 1.5)
TREND_INTERVAL    = "15m"
TREND_PERIODS     = 3
TREND_BONUS       = 2

# Binance symbols
BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
}

MARKETS = {
    "btc-updown-5m": "BTC",
    "eth-updown-5m": "ETH",
}
ENABLED_COINS      = set(get_enabled_coins(_bot_settings))
ACTIVE_MARKETS     = {prefix: coin for prefix, coin in MARKETS.items() if coin in ENABLED_COINS}

# ─── UTILS ─────────────────────────────────────────────────────────────────────
def ts_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def log(msg):
    print(f"[{ts_str()}] {msg}")

def now_unix():
    return int(time.time())

def next_close_ts():
    return ((now_unix() // 300) + 1) * 300

def window_open_ts():
    """Timestamp of the current period's open (multiple of 300)."""
    return (now_unix() // 300) * 300

# ─── BINANCE API ───────────────────────────────────────────────────────────────
def get_binance_candles(symbol: str, interval: str = "1m", limit: int = 6) -> list:
    """Fetches the last N candles from Binance."""
    try:
        r = requests.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=3
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"[BINANCE ERROR] {e}")
        return []

def get_binance_price(symbol: str) -> float:
    """Current price from Binance."""
    try:
        r = requests.get(
            f"{BINANCE_API}/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=2
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return 0.0

def get_window_open_price(symbol: str, window_ts: int) -> float:
    """
    Fetches the open price of the current period from Binance.
    window_ts is the Unix timestamp of the 5-minute period start.
    """
    try:
        # Fetch the 5min candle corresponding to the period start
        r = requests.get(
            f"{BINANCE_API}/api/v3/klines",
            params={
                "symbol":    symbol,
                "interval":  "5m",
                "startTime": window_ts * 1000,  # Binance uses milliseconds
                "limit":     1,
            },
            timeout=3
        )
        r.raise_for_status()
        candles = r.json()
        if candles:
            return float(candles[0][1])  # open price
        return 0.0
    except Exception:
        return 0.0

# ─── TECHNICAL ANALYSIS ────────────────────────────────────────────────────────

def get_atr(symbol: str, window_ts: int, periods: int = 5) -> float:
    """
    Calculates ATR (Average True Range) over the last N 5min periods.
    Returns the average range in USDC.
    """
    try:
        # Fetch periods candles ending at the current period start
        r = requests.get(
            f"{BINANCE_API}/api/v3/klines",
            params={
                "symbol":   symbol,
                "interval": "5m",
                "endTime":  window_ts * 1000,  # up to the current period start
                "limit":    periods,
            },
            timeout=3
        )
        r.raise_for_status()
        candles = r.json()
        if not candles:
            return 0.0
        ranges = [float(c[2]) - float(c[3]) for c in candles]  # high - low
        return sum(ranges) / len(ranges)
    except Exception:
        return 0.0

def get_higher_timeframe_trend(symbol: str, interval: str = TREND_INTERVAL,
                               periods: int = TREND_PERIODS) -> str | None:
    """Simple higher timeframe trend confirmation from Binance candles."""
    try:
        candles = get_binance_candles(symbol, interval, periods)
        if len(candles) < periods:
            return None
        first_open = float(candles[0][1])
        last_close = float(candles[-1][4])
        if last_close > first_open:
            return "Up"
        if last_close < first_open:
            return "Down"
        return None
    except Exception:
        return None

def analyze_micro_momentum(candles: list) -> tuple[float, str]:
    """Weighted 5-candle micro momentum. Newer candles have more weight."""
    if len(candles) < 5:
        return 0.0, "no data"

    closes = [float(c[4]) for c in candles[-5:]]
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    directions = [1 if change > 0 else (-1 if change < 0 else 0) for change in changes]
    weights = [0.1, 0.2, 0.3, 0.4]
    weighted_sum = sum(directions[i] * weights[i] for i in range(len(directions)))

    if weighted_sum > 0:
        return min(weighted_sum * 3, 3), f"Up ({min(weighted_sum * 3, 3):.1f})"
    if weighted_sum < 0:
        return max(weighted_sum * 3, -3), f"Down ({min(abs(weighted_sum) * 3, 3):.1f})"
    return 0.0, "Flat (0.0)"

def analyze(symbol: str, window_ts: int) -> dict:
    """
    Calculates a composite score based on:
    1. Window Delta (weight 5–7) — difference between current price and period open
    2. Micro momentum (weight 2) — direction of last 2 1min candles

    Returns: {score, confidence, direction, window_open, current_price, delta_pct, reason}
    """
    # Current price
    current_price = get_binance_price(symbol)
    if current_price <= 0:
        return {"confidence": 0, "direction": None, "reason": "no Binance price"}

    # Period open price
    window_open = get_window_open_price(symbol, window_ts)
    if window_open <= 0:
        # Fallback: use the open of the first 1min candle in the period
        candles = get_binance_candles(symbol, "1m", 6)
        if candles:
            window_open = float(candles[0][1])
        else:
            return {"confidence": 0, "direction": None, "reason": "no open price"}

    # 1. Window Delta
    delta = (current_price - window_open) / window_open
    delta_pct = abs(delta) * 100
    delta_dir = "Up" if delta > 0 else "Down"

    # ATR — volatility filter
    # If the current period range already exceeds 1.5x historical ATR → too volatile
    atr = get_atr(symbol, window_ts, ATR_PERIODS)
    if atr > 0:
        candles_5m = get_binance_candles(symbol, "5m", 1)
        if candles_5m:
            current_range = float(candles_5m[0][2]) - float(candles_5m[0][3])  # high - low
            if current_range > atr * ATR_MULTIPLIER:
                return {
                    "confidence":    0,
                    "direction":     None,
                    "window_open":   window_open,
                    "current_price": current_price,
                    "delta_pct":     delta_pct,
                    "atr":           atr,
                    "current_range": current_range,
                    "reason":        f"ATR skip: range ${current_range:.2f} > {ATR_MULTIPLIER}x ATR ${atr:.2f}",
                }

    if abs(delta) < DELTA_SKIP:
        return {
            "confidence":    0,
            "direction":     None,
            "window_open":   window_open,
            "current_price": current_price,
            "delta_pct":     delta_pct,
            "reason":        f"delta {delta_pct:.4f}% < {DELTA_SKIP*100:.3f}% — too close to the line",
        }

    # Delta weight
    if abs(delta) >= DELTA_STRONG * 5:  # > 1%
        delta_weight = 7
    elif abs(delta) >= DELTA_STRONG:    # > 0.2%
        delta_weight = 5
    elif abs(delta) >= DELTA_WEAK:      # > 0.1%
        delta_weight = 3
    else:                                # > 0.05%
        delta_weight = 1

    score = delta_weight if delta > 0 else -delta_weight

    # 2. Weighted micro momentum (last 5 x 1m candles)
    candles = get_binance_candles(symbol, "1m", 6)
    momentum_weight, momentum_desc = analyze_micro_momentum(candles)
    if (delta > 0 and momentum_weight > 0) or (delta < 0 and momentum_weight < 0):
        score += abs(momentum_weight)
        momentum_str = f"{momentum_desc} (confirms)"
    elif momentum_weight != 0:
        momentum_str = f"{momentum_desc} (contradicts, ignored)"
    else:
        momentum_str = momentum_desc

    # 3. Higher timeframe trend confirmation.
    # Stronger signals get a small bonus when the 15m context agrees.
    higher_trend = get_higher_timeframe_trend(symbol)
    if higher_trend and ((delta > 0 and higher_trend == "Up") or (delta < 0 and higher_trend == "Down")):
        score += TREND_BONUS
        trend_str = f"{higher_trend} (+{TREND_BONUS})"
    elif higher_trend:
        trend_str = f"{higher_trend} (contradicts)"
        if abs(delta) < DELTA_STRONG:
            return {
                "confidence":    0,
                "direction":     None,
                "window_open":   window_open,
                "current_price": current_price,
                "delta_pct":     delta_pct,
                "delta_weight":  delta_weight,
                "momentum":      momentum_str,
                "higher_trend":  trend_str,
                "atr":           atr if 'atr' in locals() else 0,
                "reason":        f"trend conflict on weak delta: {trend_str}",
            }
    else:
        trend_str = "unknown"

    # Confidence normalized over max score:
    # delta 7 + momentum 3 + trend 2 = 12
    confidence = min(abs(score) / 12.0, 1.0)
    direction  = "Up" if score > 0 else "Down"

    return {
        "score":         score,
        "confidence":    confidence,
        "direction":     direction,
        "window_open":   window_open,
        "current_price": current_price,
        "delta_pct":     delta_pct,
        "delta_weight":  delta_weight,
        "momentum":      momentum_str,
        "higher_trend":  trend_str,
        "atr":           atr if 'atr' in locals() else 0,
        "reason":        f"delta={delta_pct:.4f}% ({delta_dir}, w={delta_weight}) momentum={momentum_str} trend={trend_str}",
    }


def estimate_model_prob(direction: str | None, market_side: str, confidence: float) -> float:
    """Estimate fair probability for the current PM side from signal direction strength.

    This is intentionally conservative and diagnostic-first. The current bot does
    not have a calibrated probability model yet, so we map signal strength into
    a narrow probability band around 50%.
    """
    confidence = max(0.0, min(float(confidence or 0.0), 1.0))
    edge_span = 0.18 * confidence
    if direction == market_side:
        return min(0.5 + edge_span, 0.99)
    if direction and direction != market_side:
        return max(0.5 - edge_span, 0.01)
    return 0.5

# ─── POLYMARKET API ────────────────────────────────────────────────────────────
def get_market_for_close(slug_prefix: str, close_ts: int) -> dict | None:
    start_ts = close_ts - 300
    slug = f"{slug_prefix}-{start_ts}"
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=3)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        event = data[0]
    except Exception:
        return None

    if not event.get("active") or event.get("closed"):
        return None

    markets = event.get("markets", [])
    if not markets:
        return None

    market = markets[0]
    outcome_prices = json.loads(market.get("outcomePrices", "[]"))
    outcomes       = json.loads(market.get("outcomes", "[]"))
    clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))

    if len(outcome_prices) < 2 or len(clob_token_ids) < 2:
        return None

    prices = [float(p) for p in outcome_prices]
    winner_idx = 0 if prices[0] >= prices[1] else 1

    return {
        "slug":         slug,
        "slug_prefix":  slug_prefix,
        "crypto":       MARKETS[slug_prefix],
        "title":        event.get("title", ""),
        "close_ts":     close_ts,
        "winner_side":  outcomes[winner_idx],
        "winner_price": prices[winner_idx],
        "winner_token": clob_token_ids[winner_idx],
        "loser_price":  prices[1 - winner_idx],
        "condition_id": market.get("conditionId", ""),
        "liquidity":    float(event.get("liquidity", 0)),
    }

def get_clob_price(token_id: str) -> float:
    try:
        r = requests.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=2)
        r.raise_for_status()
        return float(r.json().get("mid", 0))
    except Exception:
        return 0.0

def execute_buy(token_id: str, amount_usdc: float, price: float,
                private_key: str, proxy_wallet: str) -> bool:
    try:
        import importlib

        if amount_usdc <= 0:
            log("   ❌ BUY failed: amount_usdc must be > 0")
            return False
        if price <= 0:
            log("   ❌ BUY failed: price must be > 0")
            return False

        client_module = importlib.import_module("py_clob_client.client")
        clob_types_module = importlib.import_module("py_clob_client.clob_types")
        constants_module = importlib.import_module("py_clob_client.order_builder.constants")

        ClobClient = client_module.ClobClient
        OrderArgs = clob_types_module.OrderArgs
        MarketOrderArgs = getattr(clob_types_module, "MarketOrderArgs", None)
        OrderType = getattr(clob_types_module, "OrderType", None)
        BUY = constants_module.BUY
        signature_type_raw = os.getenv("POLY_SIGNATURE_TYPE")
        if signature_type_raw is not None:
            signature_type = int(signature_type_raw)
        else:
            signature_type = 2 if proxy_wallet else 0

        client = ClobClient(
            host=CLOB_API,
            key=private_key,
            chain_id=137,
            signature_type=signature_type,
            funder=proxy_wallet,
        )
        client.set_api_creds(client.create_or_derive_api_creds())

        resp = None
        if MarketOrderArgs is not None and OrderType is not None:
            market_order = client.create_market_order(MarketOrderArgs(
                token_id=token_id,
                amount=round(amount_usdc, 2),
                side=BUY,
                order_type=OrderType.FOK,
            ))
            resp = client.post_order(market_order, orderType=OrderType.FOK)
        else:
            taker_price = min(round(price + 0.01, 2), 0.99)  # multiple of 0.01, max 0.99
            size = round(amount_usdc / price, 2)

            if size <= 0:
                log("   ❌ BUY failed: computed order size must be > 0")
                return False

            resp = client.create_and_post_order(OrderArgs(
                token_id=token_id,
                price=taker_price,
                size=size,
                side=BUY,
            ))

        status = resp.get("status") if isinstance(resp, dict) else "ok"
        order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
        if status == "matched":
            log(f"   ✅ BUY OK: {status} | order {order_id[:20]}...")
            return True

        log(f"   ❌ BUY not filled immediately: {status} | order {order_id[:20]}...")
        return False
    except Exception as e:
        log(f"   ❌ BUY failed: {e}")
        return False


def get_collateral_balance_allowance(private_key: str, proxy_wallet: str) -> tuple[float | None, float | None]:
    """Return available collateral balance/allowance from Polymarket, if available."""
    try:
        import importlib

        client_module = importlib.import_module("py_clob_client.client")
        clob_types_module = importlib.import_module("py_clob_client.clob_types")

        ClobClient = client_module.ClobClient
        BalanceAllowanceParams = clob_types_module.BalanceAllowanceParams
        AssetType = clob_types_module.AssetType

        signature_type_raw = os.getenv("POLY_SIGNATURE_TYPE")
        if signature_type_raw is not None:
            signature_type = int(signature_type_raw)
        else:
            signature_type = 2 if proxy_wallet else 0

        client = ClobClient(
            host=CLOB_API,
            key=private_key,
            chain_id=137,
            signature_type=signature_type,
            funder=proxy_wallet,
        )
        client.set_api_creds(client.create_or_derive_api_creds())

        raw = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )

        if not isinstance(raw, dict):
            return None, None

        def _to_float(value):
            try:
                if value is None:
                    return None
                return float(value)
            except Exception:
                return None

        balance = _to_float(raw.get("balance"))
        allowance = _to_float(raw.get("allowance"))

        if allowance is None:
            nested_allowances = raw.get("allowances") or raw.get("allowanceData") or {}
            if isinstance(nested_allowances, dict):
                values = [_to_float(v) for v in nested_allowances.values()]
                values = [v for v in values if v is not None]
                if values:
                    allowance = max(values)

        return balance, allowance
    except Exception:
        return None, None

# ─── BOT ───────────────────────────────────────────────────────────────────────
class CryptoBot:

    def __init__(self, paper: bool, dry_run: bool, amount: float):
        self.paper        = paper
        self.dry_run      = dry_run  # real data, no execution
        self.amount       = amount
        self.traded_slugs = set()
        self.trades       = []
        self.private_key  = os.getenv("POLY_PRIVATE_KEY", "")
        self.proxy_wallet = os.getenv("POLY_PROXY_WALLET", "")
        self.running      = True  # Flag for control

        # Банк (начальный капитал) и текущий баланс
        settings = load_settings()
        self.bank_start = float(settings.get("bank", 100.0))
        self.bank_balance = self.bank_start
        self.daily_loss_limit = float(settings.get("daily_loss_limit", 15.0))
        self.daily_loss_limit_pct = float(settings.get("daily_loss_limit_pct", 0.0) or 0.0)
        self.dynamic_sizing = bool(settings.get("dynamic_sizing", True))
        self.dynamic_min_amount = float(settings.get("dynamic_min_amount", 5.0))
        self.dynamic_max_amount = float(settings.get("dynamic_max_amount", 15.0))
        self.dynamic_base_risk_pct = float(settings.get("dynamic_base_risk_pct", 0.05))
        self.dynamic_step_bank_gain_pct = float(settings.get("dynamic_step_bank_gain_pct", 0.70))
        self.dynamic_step_risk_pct = float(settings.get("dynamic_step_risk_pct", 0.01))
        self.dynamic_max_risk_pct = float(settings.get("dynamic_max_risk_pct", 0.08))

        # Prevent duplicate bot processes before continuing.
        try:
            ensure_single_instance()
        except Exception as e:
            raise RuntimeError(f"Failed to acquire single-instance lock: {e}") from e

        if not paper and not dry_run and (not self.private_key or not self.proxy_wallet):
            raise ValueError("POLY_PRIVATE_KEY and POLY_PROXY_WALLET required in .env")

        mode = "DRY RUN" if dry_run else ("PAPER" if paper else "🔴 LIVE")
        current_trade_amount = self._get_trade_amount()
        log("=" * 60)
        if self.dynamic_sizing:
            log(
                f"Crypto Up/Down Bot | {mode} | base=${self.amount:.2f}/trade "
                f"| current=${current_trade_amount:.2f}/trade"
            )
        else:
            log(f"Crypto Up/Down Bot | {mode} | ${current_trade_amount:.2f}/trade")
        log(f"Bank: ${self.bank_start:.2f} | Markets: {', '.join(ACTIVE_MARKETS.values())}")
        log(f"Entry window: {ENTRY_SECONDS_MIN}-{ENTRY_SECONDS_MAX}s | "
            f"Price: BTC>={PRICE_MIN['BTC']} ETH>={PRICE_MIN['ETH']} max={PRICE_MAX}")
        log(f"Min delta: {DELTA_SKIP*100:.3f}% | Min confidence: {MIN_CONFIDENCE*100:.0f}% | ATR: {ATR_MULTIPLIER}x")
        if self.daily_loss_limit_pct > 0:
            effective_loss_limit = self._effective_daily_loss_limit()
            log(
                f"Daily loss limit: ${effective_loss_limit:.2f} "
                f"({self.daily_loss_limit_pct*100:.0f}% of bank, floor ${self.daily_loss_limit:.2f})"
            )
        else:
            log(f"Daily loss limit: ${self.daily_loss_limit:.2f}")
        if self.dynamic_sizing:
            log(
                "Dynamic sizing: "
                f"{self.dynamic_base_risk_pct*100:.0f}% +{self.dynamic_step_risk_pct*100:.0f}% per "
                f"+{self.dynamic_step_bank_gain_pct*100:.0f}% bank growth, "
                f"max {self.dynamic_max_risk_pct*100:.0f}% | cap ${self.dynamic_max_amount:.2f}"
            )
        log(f"Settings from: {'settings.json' if _bot_settings else 'defaults'}")
        log("=" * 60)

    def _daily_loss_limit_hit(self) -> bool:
        """Stop new entries once the configured daily loss limit is reached."""
        realized_pnl = self.bank_balance - self.bank_start
        return realized_pnl <= -abs(self._effective_daily_loss_limit())

    def _effective_daily_loss_limit(self) -> float:
        """Return the active daily loss limit, optionally scaled by starting bank."""
        base_limit = abs(self.daily_loss_limit)
        if self.daily_loss_limit_pct > 0 and self.bank_start > 0:
            return min(base_limit, self.bank_start * self.daily_loss_limit_pct)
        return base_limit

    def _get_trade_amount(self) -> float:
        """Return the current trade amount based on bank growth and sizing rules."""
        if not self.dynamic_sizing:
            return self.amount

        growth = 0.0
        if self.bank_start > 0:
            growth = max((self.bank_balance - self.bank_start) / self.bank_start, 0.0)

        steps = 0
        if self.dynamic_step_bank_gain_pct > 0:
            steps = int(growth / self.dynamic_step_bank_gain_pct)

        risk_pct = min(
            self.dynamic_base_risk_pct + (steps * self.dynamic_step_risk_pct),
            self.dynamic_max_risk_pct,
        )
        sized_amount = self.bank_balance * risk_pct
        return max(self.dynamic_min_amount, min(sized_amount, self.dynamic_max_amount))

    def run(self):
        while self.running:
            try:
                # Check for control commands
                cmd = check_control()
                if cmd == "stop":
                    log("⏹️ Stop command received from dashboard")
                    CONTROL_FILE.unlink(missing_ok=True)
                    break
                elif cmd == "restart":
                    log("🔄 Restart command received from dashboard")
                    CONTROL_FILE.unlink(missing_ok=True)
                    log("Restarting...")

                self._cycle()
            except KeyboardInterrupt:
                log("Stopped.")
                self._print_summary()
                break
            except Exception as e:
                log(f"Error: {e}")
                time.sleep(5)
        self._cleanup()

    def _cleanup(self):
        """Clean up PID file on exit."""
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        self._print_summary()

    def _cycle(self):
        if not ACTIVE_MARKETS:
            log("No active markets configured. Sleeping.")
            time.sleep(POLL_INTERVAL)
            return

        close_ts   = next_close_ts()
        sleep_secs = close_ts - now_unix() - WAKE_BEFORE

        if sleep_secs > 0:
            log(f"💤 Sleeping {sleep_secs:.0f}s → next close "
                f"{datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
            time.sleep(sleep_secs)

        if now_unix() >= close_ts + 5:
            log(f"⚠️  Arrived too late, skipping close "
                f"{datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
            for prefix in ACTIVE_MARKETS:
                self.traded_slugs.add(f"{prefix}-{close_ts - 300}")
            return

        log(f"⚡ Active window — close "
            f"{datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC")

        entered_slugs = set()

        while True:
            seconds_left = close_ts - now_unix()

            if seconds_left <= 0:
                log("⏰ Market closed.")
                for prefix in ACTIVE_MARKETS:
                    self.traded_slugs.add(f"{prefix}-{close_ts - 300}")
                # Проверяем результаты только что закрывшегося раунда.
                # Раньше здесь использовался предыдущий close_ts, из-за чего
                # сделки могли сверяться с неправильным рынком Polymarket.
                self._check_previous_round(close_ts)
                break

            pending = [
                prefix for prefix in MARKETS
                if prefix in ACTIVE_MARKETS
                if f"{prefix}-{close_ts - 300}" not in self.traded_slugs
                and f"{prefix}-{close_ts - 300}" not in entered_slugs
            ]

            if not pending:
                time.sleep(POLL_INTERVAL)
                continue

            # Query Polymarket and Binance in parallel
            def fetch_all(prefix):
                market = get_market_for_close(prefix, close_ts)
                if not market:
                    return prefix, None, None
                clob_price = get_clob_price(market["winner_token"])
                if clob_price > 0:
                    market["winner_price"] = clob_price
                # Technical analysis with Binance — correct symbol per crypto
                w_ts = close_ts - 300
                crypto_name = MARKETS[prefix]
                binance_sym = BINANCE_SYMBOLS.get(crypto_name, "BTCUSDT")
                ta = analyze(binance_sym, w_ts)
                return prefix, market, ta

            with ThreadPoolExecutor(max_workers=len(pending)) as executor:
                futures = {executor.submit(fetch_all, p): p for p in pending}
                results = []
                for future in as_completed(futures):
                    results.append(future.result())

            seconds_left = close_ts - now_unix()

            for prefix, market, ta in results:
                if not market or not ta:
                    continue

                slug   = market["slug"]
                crypto = market["crypto"]

                if slug in self.traded_slugs or slug in entered_slugs:
                    continue

                # Log monitoring info when still outside entry window
                if seconds_left > ENTRY_SECONDS_MAX + 5:
                    log(f"   [{crypto}] {seconds_left:.0f}s | "
                        f"PM:{market['winner_side']}@{market['winner_price']:.3f} | "
                        f"Price:{ta.get('current_price',0):.2f} | "
                        f"delta:{ta.get('delta_pct',0):.4f}% | "
                        f"conf:{ta.get('confidence',0):.0%}")
                    continue

                log(f"🎯 [{crypto}] {seconds_left:.1f}s | "
                    f"PM:{market['winner_side']}@{market['winner_price']:.3f} | "
                    f"Price:{ta.get('current_price',0):.2f} | "
                    f"delta:{ta.get('delta_pct',0):.4f}% | "
                    f"conf:{ta.get('confidence',0):.0%} | "
                    f"{ta.get('reason','')[:50]}")

                if ENTRY_SECONDS_MIN <= seconds_left <= ENTRY_SECONDS_MAX:
                    self._evaluate_entry(market, ta, seconds_left, entered_slugs)

            time.sleep(POLL_INTERVAL)

    def _evaluate_entry(self, market, ta, seconds_left, entered_slugs):
        slug      = market["slug"]
        crypto    = market["crypto"]
        price_min = PRICE_MIN.get(crypto, 0.92)
        trade_amount = self._get_trade_amount()
        market_prob = float(market["winner_price"] or 0)
        model_prob = estimate_model_prob(
            ta.get("direction"),
            market["winner_side"],
            ta.get("confidence", 0),
        )
        edge = model_prob - market_prob

        # Build signal data for saving
        signal_data = {
            "timestamp": ts_str(),
            "market_slug": slug,
            "market_close_ts": market.get("close_ts"),
            "market_start_ts": market.get("close_ts", 0) - 300 if market.get("close_ts") else None,
            "coin": crypto,
            "side": market["winner_side"],
            "pm": market["winner_price"],
            "delta": ta.get("delta_pct", 0),
            "confidence": ta.get("confidence", 0),
            "score": ta.get("score", 0),
            "model_prob": model_prob,
            "market_prob": market_prob,
            "edge": edge,
            "price": ta.get("current_price", 0),
            "time_left": seconds_left,
            "entered": False,
            "reason": "",
            "amount": trade_amount,
        }

        if self._daily_loss_limit_hit():
            log(f"   [{crypto}] SKIP — daily loss limit reached (${self.daily_loss_limit:.2f})")
            signal_data["reason"] = f"daily loss limit reached ({self.daily_loss_limit:.2f})"
            save_signal(signal_data)
            return

        # Filter 1: minimum price per crypto
        if market["winner_price"] < price_min:
            log(f"   [{crypto}] SKIP — PM price {market['winner_price']:.3f} < {price_min}")
            signal_data["reason"] = f"PM price < {price_min}"
            save_signal(signal_data)
            return

        # Filter 1b: maximum price
        if market["winner_price"] > PRICE_MAX:
            log(f'   [{crypto}] SKIP — PM price {market["winner_price"]:.3f} > {PRICE_MAX} (minimal upside)')
            signal_data["reason"] = f"PM price > {PRICE_MAX}"
            save_signal(signal_data)
            return

        # Filter 2: minimum confidence
        confidence = ta.get("confidence", 0)
        if confidence < MIN_CONFIDENCE:
            log(f"   [{crypto}] SKIP — confidence {confidence:.0%} < {MIN_CONFIDENCE:.0%}")
            signal_data["reason"] = f"confidence < {MIN_CONFIDENCE:.0%}"
            save_signal(signal_data)
            return

        # Filter 3: direction must match between Binance and Polymarket
        ta_dir  = ta.get("direction")
        pm_side = market["winner_side"]
        if ta_dir and ta_dir != pm_side:
            log(f"   [{crypto}] SKIP — Binance says {ta_dir} but PM says {pm_side}")
            signal_data["reason"] = f"direction mismatch: {ta_dir} vs {pm_side}"
            save_signal(signal_data)
            return

        # Filter 4: minimum delta
        delta_pct = ta.get("delta_pct", 0)
        if delta_pct < DELTA_SKIP * 100:
            log(f"   [{crypto}] SKIP — delta {delta_pct:.4f}% too small")
            signal_data["reason"] = f"delta {delta_pct:.4f}% too small"
            save_signal(signal_data)
            return

        if not self.paper and not self.dry_run:
            collateral_balance, collateral_allowance = get_collateral_balance_allowance(
                self.private_key,
                self.proxy_wallet,
            )
            spendable_limits = [v for v in (collateral_balance, collateral_allowance) if v is not None]
            if spendable_limits and min(spendable_limits) + 1e-9 < trade_amount:
                spendable = min(spendable_limits)
                log(
                    f"   [{crypto}] SKIP — insufficient collateral/allowance ${spendable:.2f} < ${trade_amount:.2f}"
                )
                signal_data["reason"] = f"insufficient collateral/allowance ({spendable:.2f} < {trade_amount:.2f})"
                save_signal(signal_data)
                return

        # Entry approved. Mark it as entered only after a successful execution.
        expected_pnl = (trade_amount / market["winner_price"]) - trade_amount
        signal_data["pnl_expected"] = expected_pnl

        executed = self._enter(market, ta, seconds_left, trade_amount)
        if executed:
            signal_data["entered"] = True
            signal_data["reason"] = "all filters passed"
            entered_slugs.add(slug)
            self.traded_slugs.add(slug)
        else:
            signal_data["entered"] = False
            signal_data["reason"] = "execution failed"

        save_signal(signal_data)

    def _enter(self, market: dict, ta: dict, seconds_left: float, trade_amount: float) -> bool:
        price        = market["winner_price"]
        expected_pnl = (trade_amount / price) - trade_amount
        expected_pct = expected_pnl / trade_amount * 100
        crypto       = market["crypto"]
        market_prob  = float(price or 0)
        model_prob   = estimate_model_prob(
            ta.get("direction"),
            market["winner_side"],
            ta.get("confidence", 0),
        )
        edge         = model_prob - market_prob

        log(f"🟢 ENTERING [{crypto} {market['winner_side']}] invested=${trade_amount:.2f} expected_pnl=+${expected_pnl:.2f} (+{expected_pct:.1f}%)")
        log(f"   {market['title'][:60]} | price={price:.3f} | time_left={seconds_left:.1f}s")
        log(f"   Price:{ta.get('current_price',0):.2f} | "
            f"delta:{ta.get('delta_pct',0):.4f}% | "
            f"conf:{ta.get('confidence',0):.0%} | "
            f"model={model_prob:.3f} market={market_prob:.3f} edge={edge:+.3f}")

        if self.paper or self.dry_run:
            mode = "📄 PAPER" if self.paper else "🔍 DRY RUN"
            log(f"   {mode} — not executed on chain")
            executed = True
        else:
            executed = execute_buy(
                market["winner_token"], trade_amount, price,
                self.private_key, self.proxy_wallet
            )

        if executed:
            self.trades.append({
                "crypto":       crypto,
                "title":        market["title"],
                "side":         market["winner_side"],
                "price_entry":  price,
                "amount":       trade_amount,
                "seconds_left": seconds_left,
                "pnl_expected": expected_pnl,
                "delta_pct":    ta.get("delta_pct", 0),
                "confidence":   ta.get("confidence", 0),
                "score":        ta.get("score", 0),
                "model_prob":   model_prob,
                "market_prob":  market_prob,
                "edge":         edge,
                "timestamp":    ts_str(),
            })
            log(f"   ✅ Trade #{len(self.trades)} recorded [{crypto}]")

        return executed

    def _check_previous_round(self, close_ts: int):
        """
        Проверяет результаты сигналов из уже закрывшихся раундов на Polymarket.
        Обновляет signals.json с результатами:
          - для entered=True: фактический результат (WIN/LOSS, realized_pnl)
          - для entered=False: контрфактический pnl_if_entered для оффлайн-анализа
        """
        try:
            # Ищем сигналы этого раунда
            if not SIGNALS_FILE.exists():
                return
            signals = load_signals_file()
            if not isinstance(signals, list):
                return

            updated = False

            for i, sig in enumerate(signals):
                entered = bool(sig.get("entered"))

                # Уже есть результат?
                # - для entered сигналов достаточно realized_pnl
                # - для skipped сигналов достаточно установленного winner/pnl_if_entered
                if entered and "realized_pnl" in sig:
                    continue
                if (not entered) and ("winner" in sig and "pnl_if_entered" in sig):
                    continue

                # Для новых сигналов используем точную привязку к market_close_ts.
                sig_close_ts = sig.get("market_close_ts")
                if sig_close_ts is not None:
                    # Проверяем все уже закрывшиеся и еще не резолвленные сигналы,
                    # потому что Gamma может финализировать рынок не сразу.
                    if sig_close_ts > close_ts:
                        continue
                else:
                    # Fallback для старых записей без market_close_ts.
                    sig_ts_str = sig.get("timestamp", "")
                    if not sig_ts_str:
                        continue
                    try:
                        sig_dt = datetime.strptime(sig_ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        sig_unix = int(sig_dt.timestamp())
                    except Exception:
                        continue

                    # Старый формат: берем только сигналы, которые точно относятся
                    # к уже закрывшимся раундам на момент текущей проверки.
                    if sig_unix > close_ts:
                        continue

                # Получаем результат с Polymarket
                crypto = sig.get("coin", "")
                side = sig.get("side", "")
                if not crypto or not side:
                    continue

                # Определяем slug для этого раунда
                slug = sig.get("market_slug")
                if not slug:
                    prefix = "btc-updown-5m" if crypto == "BTC" else "eth-updown-5m"
                    start_ts = close_ts - 300
                    slug = f"{prefix}-{start_ts}"

                # Запрашиваем результат с Gamma API
                try:
                    r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=5)
                    r.raise_for_status()
                    data = r.json()
                    if not data:
                        continue
                    event = data[0]
                    if event.get("active") and not event.get("closed"):
                        # Рынок еще не финализирован, попробуем в следующем цикле.
                        continue
                    markets = event.get("markets", [])
                    if not markets:
                        continue
                    market = markets[0]
                    outcome_prices = json.loads(market.get("outcomePrices", "[]"))
                    outcomes = json.loads(market.get("outcomes", "[]"))

                    if len(outcome_prices) < 2 or len(outcomes) < 2:
                        continue

                    prices = [float(p) for p in outcome_prices]
                    winner_idx = 0 if prices[0] >= prices[1] else 1
                    winner = outcomes[winner_idx]
                    loser = outcomes[1 - winner_idx]
                except Exception as e:
                    log(f"   ⚠️  Result check failed for {slug}: {e}")
                    continue

                # Определяем win/loss
                won = (side == winner)
                entry_price = sig.get("pm", 0)
                trade_amount = sig.get("amount", self.amount)

                signals[i]["won"] = won
                signals[i]["winner"] = winner
                signals[i]["loser"] = loser
                signals[i]["resolved_at"] = ts_str()

                if won:
                    # Выигрыш: payout = amount / entry_price
                    payout = trade_amount / entry_price if entry_price > 0 else trade_amount
                    pnl_if_entered = payout - trade_amount
                    result = "WIN"
                else:
                    # Проигрыш: теряем ставку
                    payout = 0
                    pnl_if_entered = -trade_amount
                    result = "LOSS"

                # Поле для оффлайн-симуляций (даже если сигнал был skipped)
                signals[i]["pnl_if_entered"] = round(pnl_if_entered, 2)

                if entered:
                    signals[i]["result"] = result
                    signals[i]["realized_pnl"] = round(pnl_if_entered, 2)
                    signals[i]["payout"] = round(payout, 2)

                    # Обновляем банк только для реально entered сигналов
                    self.bank_balance += pnl_if_entered
                    log(f"   🏁 [{crypto}] {result} | side={side} winner={winner} | "
                        f"pnl=${pnl_if_entered:+.2f} | bank=${self.bank_balance:.2f}")
                else:
                    signals[i]["counterfactual_result"] = result

                updated = True

            if updated:
                save_signals_file(signals)

        except Exception as e:
            log(f"   ⚠️  _check_previous_round error: {e}")

    def _print_summary(self):
        log("─" * 60)
        log(f"SUMMARY — {len(self.trades)} trades")
        total_invested = sum(t["amount"] for t in self.trades)
        total_expected = sum(t["pnl_expected"] for t in self.trades)
        for t in self.trades:
            log(f"  [{t['crypto']}] {t['title'][:35]} | {t['side']} @ "
                f"{t['price_entry']:.3f} | {t['seconds_left']:.0f}s | "
                f"delta:{t['delta_pct']:.4f}% | conf:{t['confidence']:.0%} | "
                f"+${t['pnl_expected']:.2f}")
        if self.trades:
            log(f"  Total invested: ${total_invested:.2f}")
            log(f"  Expected PnL:   +${total_expected:.2f} "
                f"(+{total_expected/total_invested*100:.1f}%)")
        log("─" * 60)


# ─── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crypto Up/Down 5min Bot with Window Delta")
    parser.add_argument("--paper",    action="store_true", help="Paper trading mode (simulated)")
    parser.add_argument("--live",     action="store_true", help="Live trading mode (real funds)")
    parser.add_argument("--dry-run",  action="store_true", help="Dry run — real data, no trades executed")
    parser.add_argument("--amount",   type=float, default=10.0, help="USDC per trade")
    args = parser.parse_args()

    dry_run = args.dry_run
    paper   = args.paper or (not args.live and not dry_run)

    bot = CryptoBot(paper=paper, dry_run=dry_run, amount=args.amount)
    bot.run()
