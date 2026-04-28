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
from collections import deque
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
WINDOW_SAMPLES_FILE = BOT_DIR / "window_samples.json"
SETTINGS_FILE = BOT_DIR / "settings.json"
PID_FILE = BOT_DIR / "bot.pid"
CORE_EV_RULES_FILE = BOT_DIR / "core_ev_rules.json"


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


def load_window_samples_file() -> list:
    """Load full-window observation history safely."""
    if not WINDOW_SAMPLES_FILE.exists():
        return []
    try:
        data = json.loads(WINDOW_SAMPLES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_window_samples_file(samples: list):
    """Persist full-window observation history atomically."""
    atomic_write_text(WINDOW_SAMPLES_FILE, json.dumps(samples[-50000:], indent=2))


def save_window_sample(sample_data: dict):
    """Append a full-window observation sample for offline EV analysis."""
    try:
        samples = load_window_samples_file()
        samples.append(sample_data)
        save_window_samples_file(samples)
    except Exception as e:
        log(f"[WINDOW SAMPLE SAVE ERROR] {e}")


def load_core_ev_rules() -> dict:
    """Load Core EV runtime rulebook, fallback to an empty structure."""
    if not CORE_EV_RULES_FILE.exists():
        return {"buckets": {}}
    try:
        data = json.loads(CORE_EV_RULES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("buckets", {}), dict):
            return data
    except Exception:
        pass
    return {"buckets": {}}


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
PRICE_MAX_STRONG  = float(_bot_settings.get("price_max_strong", max(float(PRICE_MAX), 0.76)) or max(float(PRICE_MAX), 0.76))

OBSERVE_WINDOW_SECONDS = int(_bot_settings.get("observe_window_seconds", 305) or 305)
FULL_WINDOW_CORE_EV_ENABLED = bool(_bot_settings.get("full_window_core_ev_enabled", True))
WINDOW_SAMPLE_LOGGING_ENABLED = bool(_bot_settings.get("window_sample_logging_enabled", True))
CORE_EV_ENTRY_TIME_MIN = float(_bot_settings.get("core_ev_entry_time_min", 10) or 10)
CORE_EV_ENTRY_TIME_MAX = float(_bot_settings.get("core_ev_entry_time_max", max(10, OBSERVE_WINDOW_SECONDS)) or max(10, OBSERVE_WINDOW_SECONDS))
FULL_WINDOW_ENTRY_CONFIRM_TICKS = int(_bot_settings.get("full_window_entry_confirm_ticks", 2) or 2)
FULL_WINDOW_ENTRY_COMMIT_TIME_LEFT = float(_bot_settings.get("full_window_entry_commit_time_left", 19) or 19)
FULL_WINDOW_ENTRY_MIN_SCORE_GAIN = float(_bot_settings.get("full_window_entry_min_score_gain", 0.15) or 0.15)
WAKE_BEFORE       = int(_bot_settings.get("wake_before", max(65, OBSERVE_WINDOW_SECONDS)) or max(65, OBSERVE_WINDOW_SECONDS))
POLL_INTERVAL     = 3

DELTA_SKIP        = _bot_settings.get("delta_skip", 0.0005)
DELTA_WEAK        = 0.001
DELTA_STRONG      = 0.002
STRONG_OVERPRICE_DELTA_MIN = float(_bot_settings.get("strong_overprice_delta_min_pct", 0.010) or 0.010)
STRONG_OVERPRICE_CONFIDENCE_MIN = float(_bot_settings.get("strong_overprice_confidence_min", 0.02) or 0.02)
STRONG_OVERPRICE_EDGE_MIN = float(_bot_settings.get("strong_overprice_edge_min", -0.03) or -0.03)
STRONG_OVERPRICE_INDICATOR_CONFIRM_MIN = float(_bot_settings.get("strong_overprice_indicator_confirm_min", -0.05) or -0.05)
STRONG_OVERPRICE_TIME_LEFT_MIN = float(_bot_settings.get("strong_overprice_time_left_min", 18) or 18)
NORMAL_ZONE_MIN = float(_bot_settings.get("normal_zone_min", 0.60) or 0.60)
NORMAL_ZONE_MAX = float(_bot_settings.get("normal_zone_max", 0.70) or 0.70)
NORMAL_ZONE_DELTA_MIN = float(_bot_settings.get("normal_zone_delta_min_pct", 0.010) or 0.010)
NORMAL_ZONE_TIME_LEFT_MIN = float(_bot_settings.get("normal_zone_time_left_min", 18) or 18)
NORMAL_ZONE_EDGE_MIN = float(_bot_settings.get("normal_zone_edge_min", -0.05) or -0.05)
NORMAL_ZONE_INDICATOR_CONFIRM_MIN = float(_bot_settings.get("normal_zone_indicator_confirm_min", -0.05) or -0.05)
HYBRID_SHADOW_PM_MIN = float(_bot_settings.get("hybrid_shadow_pm_min", NORMAL_ZONE_MIN) or NORMAL_ZONE_MIN)
CORE_EV_ENABLED = bool(_bot_settings.get("core_ev_enabled", True))
CORE_EV_PM_MIN = float(_bot_settings.get("core_ev_pm_min", 0.58) or 0.58)
CORE_EV_PM_MAX = float(_bot_settings.get("core_ev_pm_max", 0.70) or 0.70)
CORE_EV_TIME_LEFT_MIN = float(_bot_settings.get("core_ev_time_left_min", CORE_EV_ENTRY_TIME_MIN) or CORE_EV_ENTRY_TIME_MIN)
CORE_EV_TIME_LEFT_MAX = float(_bot_settings.get("core_ev_time_left_max", min(20, CORE_EV_ENTRY_TIME_MAX)) or min(20, CORE_EV_ENTRY_TIME_MAX))
CORE_EV_MAX_RISK_PCT = float(_bot_settings.get("core_ev_max_risk_pct", 0.02) or 0.02)
FULL_WINDOW_CORE_EV_MIN_LEVEL = str(_bot_settings.get("full_window_core_ev_min_level", "L2") or "L2").strip().upper()
FULL_WINDOW_L1_FALLBACK_MIN_TRADES = int(_bot_settings.get("full_window_l1_fallback_min_trades", 8) or 8)
FULL_WINDOW_L1_FALLBACK_REQUIRE_RECENT_POSITIVE = bool(_bot_settings.get("full_window_l1_fallback_require_recent_positive", True))
FULL_WINDOW_L1_FALLBACK_TIME_LEFT_MAX = float(_bot_settings.get("full_window_l1_fallback_time_left_max", 150) or 150)
WINDOW_HISTORY_MAX_POINTS = int(_bot_settings.get("window_history_max_points", 140) or 140)
SHADOW_MIN_STABLE_TICKS = int(_bot_settings.get("shadow_min_stable_ticks", 3) or 3)
SHADOW_SOFT_STABLE_TICKS = int(_bot_settings.get("shadow_soft_stable_ticks", max(1, SHADOW_MIN_STABLE_TICKS - 1)) or max(1, SHADOW_MIN_STABLE_TICKS - 1))
SHADOW_EARLY_DELTA_MIN = float(_bot_settings.get("shadow_early_delta_min_pct", 0.010) or 0.010)
SHADOW_LATE_DELTA_MIN = float(_bot_settings.get("shadow_late_delta_min_pct", 0.015) or 0.015)
SHADOW_PM_MAX = float(_bot_settings.get("shadow_pm_max", 0.76) or 0.76)
SHADOW_PULLBACK_MAX = float(_bot_settings.get("shadow_pullback_max_pct", 0.012) or 0.012)
SHADOW_UNDERPRICING_MIN = float(_bot_settings.get("shadow_underpricing_min", 0.010) or 0.010)
SHADOW_PROBE_DELTA_MIN = float(_bot_settings.get("shadow_probe_delta_min_pct", SHADOW_EARLY_DELTA_MIN * 0.8) or (SHADOW_EARLY_DELTA_MIN * 0.8))
SHADOW_PROBE_PM_MAX = float(_bot_settings.get("shadow_probe_pm_max", min(SHADOW_PM_MAX, 0.70)) or min(SHADOW_PM_MAX, 0.70))
SHADOW_OBSERVE_PM_FLOOR = float(_bot_settings.get("shadow_observe_pm_floor", 0.10) or 0.10)
SHADOW_EARLY_PM_FLOOR = float(_bot_settings.get("shadow_early_pm_floor", SHADOW_OBSERVE_PM_FLOOR) or SHADOW_OBSERVE_PM_FLOOR)
SHADOW_OBSERVE_CHEAP_PM_MAX_PROGRESS = float(_bot_settings.get("shadow_observe_cheap_pm_max_progress", 0.90) or 0.90)
SHADOW_REGIME_SUPPORT_UNDERPRICING_MIN = float(_bot_settings.get("shadow_regime_support_underpricing_min", 0.05) or 0.05)
SHADOW_LIVE_ALLOW_MIN_SCORE = float(_bot_settings.get("shadow_live_allow_min_score", 4.5) or 4.5)
SHADOW_LIVE_STRONG_ALLOW_MIN_SCORE = float(_bot_settings.get("shadow_live_strong_allow_min_score", 6.0) or 6.0)
SHADOW_LIVE_DENY_MAX_PM_GAP = float(_bot_settings.get("shadow_live_deny_max_pm_gap", 0.18) or 0.18)
SHADOW_LIVE_DENY_MIN_PROGRESS = float(_bot_settings.get("shadow_live_deny_min_progress", 0.80) or 0.80)
SHADOW_LIVE_WATCH_MIN_SCORE = float(_bot_settings.get("shadow_live_watch_min_score", 3.0) or 3.0)
SHADOW_LIVE_MODE = str(_bot_settings.get("shadow_live_mode", "observe") or "observe").strip().lower()

MIN_CONFIDENCE    = _bot_settings.get("min_confidence", 0.3)
MIN_EDGE          = float(_bot_settings.get("min_edge", -0.05) or -0.05)
INDICATOR_CONFIRM_MIN = float(_bot_settings.get("indicator_confirm_min", 0.0) or 0.0)
TREND_CONFLICT_OVERRIDE_DELTA_MIN_PCT = float(_bot_settings.get("trend_conflict_override_delta_min_pct", 0.050) or 0.050)

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


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalize_shadow_live_mode(value: str) -> str:
    mode = str(value or "observe").strip().lower()
    if mode in {"off", "observe", "block_deny", "hybrid"}:
        return mode
    return "observe"


def bucket_time_left_value(time_left: float) -> str:
    if time_left < 10:
        return "<10s"
    if time_left < 15:
        return "10-14s"
    if time_left < 20:
        return "15-19s"
    if time_left < 30:
        return "20-29s"
    if time_left < 40:
        return "30-39s"
    return ">=40s"


def bucket_pm_value(pm_price: float) -> str:
    if pm_price < 0.55:
        return "<0.55"
    if pm_price < 0.58:
        return "0.55-0.579"
    if pm_price < 0.60:
        return "0.58-0.599"
    if pm_price < 0.62:
        return "0.60-0.619"
    if pm_price < 0.64:
        return "0.62-0.639"
    if pm_price < 0.67:
        return "0.64-0.669"
    if pm_price < 0.70:
        return "0.67-0.699"
    if pm_price < 0.80:
        return "0.70-0.799"
    if pm_price < 0.90:
        return "0.80-0.899"
    if pm_price < 0.94:
        return "0.90-0.939"
    if pm_price < 0.95:
        return "0.94-0.949"
    if pm_price < 0.96:
        return "0.95-0.959"
    if pm_price < 0.97:
        return "0.96-0.969"
    if pm_price < 0.98:
        return "0.97-0.979"
    return ">=0.98"


def bucket_delta_value(delta_pct: float) -> str:
    if delta_pct < 0.005:
        return "<0.005%"
    if delta_pct < 0.010:
        return "0.005-0.009%"
    if delta_pct < 0.020:
        return "0.010-0.019%"
    if delta_pct < 0.030:
        return "0.020-0.029%"
    if delta_pct < 0.050:
        return "0.030-0.049%"
    if delta_pct < 0.10:
        return "0.050-0.099%"
    if delta_pct < 0.15:
        return "0.10-0.15%"
    if delta_pct < 0.20:
        return "0.15-0.20%"
    if delta_pct < 0.30:
        return "0.20-0.30%"
    if delta_pct < 0.50:
        return "0.30-0.50%"
    return ">=0.50%"


def bucket_indicator_confirm_value(confirm: float) -> str:
    if confirm < -0.50:
        return "<-0.50"
    if confirm < -0.20:
        return "-0.50..-0.20"
    if confirm < 0.0:
        return "-0.20..0.00"
    if confirm < 0.10:
        return "0.00..0.09"
    if confirm < 0.25:
        return "0.10..0.24"
    if confirm < 0.50:
        return "0.25..0.49"
    return ">=0.50"


def bucket_stable_ticks_value(stable_ticks: int) -> str:
    if stable_ticks <= 0:
        return "0"
    if stable_ticks == 1:
        return "1"
    if stable_ticks == 2:
        return "2"
    if stable_ticks == 3:
        return "3"
    if stable_ticks == 4:
        return "4"
    return ">=5"


def core_l1_pm_bucket_value(pm_price: float) -> str:
    if pm_price < 0.62:
        return "0.58-0.619"
    return "0.62-0.70"


def core_l1_delta_bucket_value(delta_pct: float) -> str:
    if delta_pct < 0.010:
        return "<0.010%"
    if delta_pct < 0.030:
        return "0.010-0.029%"
    return ">=0.030%"


def core_l1_time_bucket_value(time_left: float) -> str:
    if time_left < 10:
        return "<10s"
    if time_left < 20:
        return "10-19s"
    if time_left < 30:
        return "20-29s"
    if time_left < 60:
        return "30-59s"
    if time_left < 120:
        return "60-119s"
    return "120-300s"

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


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    ema_values = [values[0]]
    for value in values[1:]:
        ema_values.append((value * alpha) + (ema_values[-1] * (1.0 - alpha)))
    return ema_values


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    sample = values[-period:]
    return sum(sample) / period


def _stddev(values: list[float], period: int) -> float | None:
    mean = _sma(values, period)
    if mean is None:
        return None
    sample = values[-period:]
    variance = sum((value - mean) ** 2 for value in sample) / period
    return variance ** 0.5


def analyze_indicator_confirm(candles: list, direction: str | None) -> tuple[float, str]:
    """Return a signed 0..1 confirmation score from 1m indicators.

    This is a diagnostic/confirm layer, not a calibrated probability model.
    It uses only already-closed 1m candles fetched from Binance, so there is no
    TradingView-style lookahead behavior.
    """
    if not direction or len(candles) < 26:
        return 0.0, "insufficient 1m data"

    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    last_close = closes[-1]

    bull_score = 0.0
    bear_score = 0.0
    reasons = []

    # MACD histogram sign.
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal_line = _ema(macd_line, 9)
    macd_hist = macd_line[-1] - signal_line[-1]
    if macd_hist > 0:
        bull_score += 0.30
        reasons.append("macd+0.30")
    elif macd_hist < 0:
        bear_score += 0.30
        reasons.append("macd-0.30")

    # RSI overbought/oversold.
    gains = []
    losses = []
    for prev_close, close in zip(closes[-15:-1], closes[-14:]):
        change = close - prev_close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / len(gains) if gains else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    if avg_loss == 0:
        rsi = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
    if rsi < 30:
        bull_score += 0.25
        reasons.append("rsi+0.25")
    elif rsi > 70:
        bear_score += 0.25
        reasons.append("rsi-0.25")

    # Stochastic oscillator.
    period_high = max(highs[-14:])
    period_low = min(lows[-14:])
    if period_high > period_low:
        stoch_k = ((last_close - period_low) / (period_high - period_low)) * 100.0
        if stoch_k < 20:
            bull_score += 0.25
            reasons.append("stoch+0.25")
        elif stoch_k > 80:
            bear_score += 0.25
            reasons.append("stoch-0.25")

    # Bollinger touch.
    bb_mid = _sma(closes, 20)
    bb_std = _stddev(closes, 20)
    if bb_mid is not None and bb_std is not None:
        bb_up = bb_mid + (bb_std * 2.0)
        bb_low = bb_mid - (bb_std * 2.0)
        if last_close <= bb_low:
            bull_score += 0.20
            reasons.append("bb+0.20")
        elif last_close >= bb_up:
            bear_score += 0.20
            reasons.append("bb-0.20")

    if direction == "Up":
        support = bull_score - bear_score
    else:
        support = bear_score - bull_score

    confirm_score = max(-1.0, min(support, 1.0))
    return confirm_score, ", ".join(reasons) if reasons else "neutral"


def classify_signal_tier(
    pm_price: float,
    delta_pct: float,
    confidence: float,
    indicator_confirm: float,
    edge: float,
    time_left: float,
    trend_aligned: bool,
) -> tuple[str, str]:
    """Classify the observed setup quality for later offline analysis."""
    if time_left < 10:
        return "observe", "too late for execution"
    if time_left > OBSERVE_WINDOW_SECONDS:
        return "observe", "outside observed window"
    if pm_price < 0.58:
        return "observe", "pm too cheap"
    if pm_price > 0.72:
        return "observe", "pm too expensive"
    if trend_aligned is False and indicator_confirm <= 0:
        return "observe", "no trend or 1m support"

    if (
        pm_price >= 0.60
        and pm_price <= 0.68
        and delta_pct >= 0.015
        and confidence >= 0.10
        and indicator_confirm >= 0.15
        and edge >= -0.01
        and trend_aligned
    ):
        return "trade", "aligned pm/delta/confirm"
    if (
        pm_price >= 0.58
        and pm_price <= 0.70
        and delta_pct >= 0.008
        and confidence >= 0.03
        and indicator_confirm >= 0.0
        and edge >= -0.04
    ):
        return "candidate", "usable but not elite"
    if (
        pm_price >= 0.60
        and pm_price <= 0.70
        and delta_pct >= 0.012
        and indicator_confirm > 0
    ):
        return "candidate", "pm/delta ok but weak confidence"
    if (
        pm_price >= 0.58
        and pm_price <= 0.68
        and confidence >= 0.05
        and edge >= -0.02
    ):
        return "candidate", "pm/conf ok but weak confirm"
    return "observe", "weak combined setup"

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
    candles = get_binance_candles(symbol, "1m", 30)
    momentum_weight, momentum_desc = analyze_micro_momentum(candles)
    if (delta > 0 and momentum_weight > 0) or (delta < 0 and momentum_weight < 0):
        score += momentum_weight
        momentum_str = f"{momentum_desc} (confirms)"
    elif momentum_weight != 0:
        momentum_str = f"{momentum_desc} (contradicts, ignored)"
    else:
        momentum_str = momentum_desc

    # 3. Higher timeframe trend confirmation.
    # Stronger signals get a small bonus when the 15m context agrees.
    higher_trend = get_higher_timeframe_trend(symbol)
    trend_aligned = False
    trend_conflict = False
    if higher_trend and ((delta > 0 and higher_trend == "Up") or (delta < 0 and higher_trend == "Down")):
        score += TREND_BONUS if delta > 0 else -TREND_BONUS
        trend_str = f"{higher_trend} (+{TREND_BONUS})"
        trend_aligned = True
    elif higher_trend:
        trend_str = f"{higher_trend} (contradicts)"
        trend_conflict = True
        if delta_pct < TREND_CONFLICT_OVERRIDE_DELTA_MIN_PCT:
            return {
                "confidence":    0,
                "direction":     None,
                "window_open":   window_open,
                "current_price": current_price,
                "delta_pct":     delta_pct,
                "delta_weight":  delta_weight,
                "momentum":      momentum_str,
                "higher_trend":  trend_str,
                "trend_aligned": trend_aligned,
                "trend_conflict": trend_conflict,
                "atr":           atr if 'atr' in locals() else 0,
                "reason":        f"trend conflict on weak delta: {trend_str}",
            }
        trend_aligned = True
        trend_conflict = False
        trend_str = f"{higher_trend} (contradicts, overridden by local delta)"
    else:
        trend_str = "unknown"

    # Confidence normalized over max score:
    # delta 7 + momentum 3 + trend 2 = 12
    confidence = min(abs(score) / 12.0, 1.0)
    direction  = "Up" if score > 0 else "Down"
    indicator_confirm, indicator_reason = analyze_indicator_confirm(candles, direction)
    signal_tier, signal_tier_reason = classify_signal_tier(
        pm_price=0.65,
        delta_pct=delta_pct,
        confidence=confidence,
        indicator_confirm=indicator_confirm,
        edge=0.0,
        time_left=ENTRY_SECONDS_MIN,
        trend_aligned=trend_aligned,
    )

    return {
        "score":         score,
        "confidence":    confidence,
        "direction":     direction,
        "indicator_confirm": indicator_confirm,
        "indicator_reason": indicator_reason,
        "window_open":   window_open,
        "current_price": current_price,
        "delta_pct":     delta_pct,
        "delta_weight":  delta_weight,
        "momentum":      momentum_str,
        "higher_trend":  trend_str,
        "trend_aligned": trend_aligned,
        "trend_conflict": trend_conflict,
        "signal_tier":   signal_tier,
        "signal_tier_reason": signal_tier_reason,
        "atr":           atr if 'atr' in locals() else 0,
        "reason":        f"delta={delta_pct:.4f}% ({delta_dir}, w={delta_weight}) momentum={momentum_str} trend={trend_str}",
    }


def estimate_model_prob(
    direction: str | None,
    market_side: str,
    confidence: float,
    market_prob: float,
    score: float = 0,
) -> float:
    """Estimate fair probability for the current PM side.

    Use the live PM price as the baseline prior and apply only a small,
    conservative signal-based adjustment. This keeps the metric diagnostic-first
    until we have enough live data to calibrate a real probability model.
    """
    market_prob = max(0.01, min(float(market_prob or 0.0), 0.99))
    confidence = max(0.0, min(float(confidence or 0.0), 1.0))
    score = abs(float(score or 0.0))

    if not direction:
        return market_prob

    alignment = 1.0 if direction == market_side else -1.0

    # Confidence already compresses score into 0..1. Score adds a mild boost so
    # weak 1-point deltas do not get the same adjustment as stronger composites.
    strength = min(1.0, (confidence * 0.75) + (min(score, 12.0) / 12.0 * 0.25))

    # Stronger priced favorites leave less room for an informational edge, while
    # near-even markets can tolerate a slightly larger adjustment.
    headroom = max(0.015, 0.08 - abs(market_prob - 0.5) * 0.12)
    adjustment = strength * headroom * alignment

    return max(0.01, min(market_prob + adjustment, 0.99))

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
    price_spread = abs(prices[0] - prices[1])

    return {
        "slug":         slug,
        "slug_prefix":  slug_prefix,
        "crypto":       MARKETS[slug_prefix],
        "title":        event.get("title", ""),
        "close_ts":     close_ts,
        "outcomes":     outcomes,
        "outcome_prices": prices,
        "clob_token_ids": clob_token_ids,
        "winner_side":  outcomes[winner_idx],
        "winner_price": prices[winner_idx],
        "winner_token": clob_token_ids[winner_idx],
        "loser_price":  prices[1 - winner_idx],
        "pm_price_spread": price_spread,
        "pm_price_source": "gamma",
        "clob_midpoint_refresh_count": 0,
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
                private_key: str, proxy_wallet: str) -> dict:
    result = {
        "ok": False,
        "failure_type": "unknown",
        "detail": "",
        "order_status": "",
        "order_id": "",
        "size": None,
        "taker_price": None,
    }
    try:
        import importlib

        if amount_usdc <= 0:
            result["failure_type"] = "invalid_amount"
            result["detail"] = f"amount_usdc must be > 0 (got {amount_usdc})"
            log(f"   ❌ BUY failed [{result['failure_type']}]: {result['detail']}")
            return result
        if price <= 0:
            result["failure_type"] = "invalid_price"
            result["detail"] = f"price must be > 0 (got {price})"
            log(f"   ❌ BUY failed [{result['failure_type']}]: {result['detail']}")
            return result

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
            result["taker_price"] = taker_price
            result["size"] = size

            if size <= 0:
                result["failure_type"] = "invalid_size"
                result["detail"] = f"computed order size must be > 0 (got {size})"
                log(f"   ❌ BUY failed [{result['failure_type']}]: {result['detail']}")
                return result

            resp = client.create_and_post_order(OrderArgs(
                token_id=token_id,
                price=taker_price,
                size=size,
                side=BUY,
            ))

        status = resp.get("status") if isinstance(resp, dict) else "ok"
        order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
        result["order_status"] = str(status or "")
        result["order_id"] = str(order_id or "")
        if status == "matched":
            result["ok"] = True
            result["failure_type"] = ""
            log(f"   ✅ BUY OK: {status} | order {str(order_id)[:20]}...")
            return result

        result["failure_type"] = "not_filled_immediately"
        result["detail"] = f"status={status}"
        log(
            f"   ❌ BUY not filled immediately [{result['failure_type']}]: "
            f"{status} | order {str(order_id)[:20]}..."
        )
        return result
    except Exception as e:
        result["failure_type"] = "exception"
        result["detail"] = str(e)
        log(f"   ❌ BUY failed [{result['failure_type']}]: {e}")
        return result


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
        self.window_history: dict[str, deque] = {}
        self.shadow_window_state: dict[str, dict] = {}
        self.closed_window_summaries: deque = deque(maxlen=12)
        self.core_ev_rules = load_core_ev_rules()

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
        log(
            f"Min delta: {DELTA_SKIP*100:.3f}% | Min confidence: {MIN_CONFIDENCE*100:.0f}% | "
            f"Min edge: {MIN_EDGE:+.3f} | 1m confirm: {INDICATOR_CONFIRM_MIN:+.2f} | ATR: {ATR_MULTIPLIER}x"
        )
        if self.daily_loss_limit_pct > 0:
            effective_loss_limit = self._effective_daily_loss_limit()
            log(
                f"Daily loss limit: ${effective_loss_limit:.2f} "
                f"({self.daily_loss_limit_pct*100:.0f}% of bank, floor ${self.daily_loss_limit:.2f})"
            )
        else:
            log(f"Daily loss limit: ${self.daily_loss_limit:.2f}")
        log(f"Trend conflict override delta: {TREND_CONFLICT_OVERRIDE_DELTA_MIN_PCT:.3f}%")
        if self.dynamic_sizing:
            log(
                "Dynamic sizing: "
                f"{self.dynamic_base_risk_pct*100:.0f}% +{self.dynamic_step_risk_pct*100:.0f}% per "
                f"+{self.dynamic_step_bank_gain_pct*100:.0f}% bank growth, "
                f"max {self.dynamic_max_risk_pct*100:.0f}% | cap ${self.dynamic_max_amount:.2f}"
            )
        log(f"Shadow live mode: {normalize_shadow_live_mode(SHADOW_LIVE_MODE)}")
        if CORE_EV_ENABLED:
            log(
                f"Core EV mode: ON | pm={CORE_EV_PM_MIN:.2f}-{CORE_EV_PM_MAX:.2f} | "
                f"rules={len(self.core_ev_rules.get('buckets', {}))} buckets"
            )
            if FULL_WINDOW_CORE_EV_ENABLED:
                log(f"Full-window Core EV min bucket level: {FULL_WINDOW_CORE_EV_MIN_LEVEL}")
                log(
                    "Full-window L1 fallback: "
                    f"min_trades={FULL_WINDOW_L1_FALLBACK_MIN_TRADES} "
                    f"recent_positive={'ON' if FULL_WINDOW_L1_FALLBACK_REQUIRE_RECENT_POSITIVE else 'OFF'} "
                    f"time_left<={FULL_WINDOW_L1_FALLBACK_TIME_LEFT_MAX:.0f}s"
                )
            if not self.core_ev_rules.get("buckets"):
                log("WARNING: Core EV rulebook is empty; rebuild core_ev_rules.json from fresh shadow-era signals before live trading.")
        else:
            log("Core EV mode: OFF")
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

    def _build_signal_snapshot(self, market, ta, seconds_left, shadow_context: dict | None = None) -> dict:
        slug = market["slug"]
        crypto = market["crypto"]
        trade_amount = self._get_trade_amount()
        market_prob = float(market["winner_price"] or 0)
        pm_price_spread = float(market.get("pm_price_spread", 0) or 0)
        pm_price_source = str(market.get("pm_price_source", "gamma") or "gamma")
        clob_midpoint_refresh_count = int(market.get("clob_midpoint_refresh_count", 0) or 0)
        model_prob = estimate_model_prob(
            ta.get("direction"),
            market["winner_side"],
            ta.get("confidence", 0),
            market_prob,
            ta.get("score", 0),
        )
        edge = model_prob - market_prob
        trend_aligned = bool(ta.get("trend_aligned"))
        trend_conflict = bool(ta.get("trend_conflict"))
        signal_tier, signal_tier_reason = classify_signal_tier(
            pm_price=market_prob,
            delta_pct=float(ta.get("delta_pct", 0) or 0),
            confidence=float(ta.get("confidence", 0) or 0),
            indicator_confirm=float(ta.get("indicator_confirm", 0) or 0),
            edge=float(edge or 0),
            time_left=float(seconds_left or 0),
            trend_aligned=trend_aligned,
        )
        shadow_context = shadow_context or self._observe_shadow_window(market, ta, seconds_left, allow_log=False)
        window_features = shadow_context.get("features", {})
        shadow = shadow_context.get("shadow", {})
        shadow_live = shadow_context.get("shadow_live", {})
        shadow_state = self.shadow_window_state.get(slug, {})

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
            "indicator_confirm": ta.get("indicator_confirm", 0),
            "indicator_reason": ta.get("indicator_reason", ""),
            "momentum": ta.get("momentum", ""),
            "higher_trend": ta.get("higher_trend", "unknown"),
            "trend_aligned": trend_aligned,
            "trend_conflict": trend_conflict,
            "signal_tier": signal_tier,
            "signal_tier_reason": signal_tier_reason,
            "model_prob": model_prob,
            "market_prob": market_prob,
            "pm_price_spread": pm_price_spread,
            "pm_price_source": pm_price_source,
            "clob_midpoint_refresh_count": clob_midpoint_refresh_count,
            "edge": edge,
            "price": ta.get("current_price", 0),
            "time_left": seconds_left,
            "entered": False,
            "reason": "",
            "amount": trade_amount,
            "execution_failure_type": "",
            "execution_failure_detail": "",
            "execution_order_status": "",
            "execution_order_id": "",
            "stable_ticks": window_features.get("stable_ticks", 0),
            "direction_persistence": window_features.get("direction_persistence", 0.0),
            "delta_slope": window_features.get("delta_slope", 0.0),
            "pm_slope": window_features.get("pm_slope", 0.0),
            "pullback_size": window_features.get("pullback_size", 0.0),
            "pullback_recovered": window_features.get("pullback_recovered", False),
            "reversal_flag": window_features.get("reversal_flag", False),
            "window_progress_pct": window_features.get("window_progress_pct", 0.0),
            "recent_5m_streak": window_features.get("recent_5m_streak", 0),
            "market_regime": window_features.get("market_regime", "unknown"),
            "pm_vs_delta_gap": window_features.get("pm_vs_delta_gap", 0.0),
            "underpricing_score": window_features.get("underpricing_score", 0.0),
            "shadow_entry_candidate": shadow.get("candidate", False),
            "shadow_entry_profile": shadow.get("profile", "none"),
            "shadow_entry_score": shadow.get("score", 0.0),
            "shadow_entry_reason": shadow.get("reason", ""),
            "shadow_live_decision": shadow_live.get("decision", "neutral"),
            "shadow_live_reason": shadow_live.get("reason", ""),
            "shadow_live_score": shadow_live.get("score", 0.0),
            "shadow_observation_count": shadow_state.get("observation_count", 0),
            "shadow_first_observed_progress_pct": shadow_state.get("first_progress_pct", window_features.get("window_progress_pct", 0.0)),
            "shadow_first_candidate_progress_pct": shadow_state.get("first_candidate_progress_pct"),
            "shadow_first_candidate_seconds_left": shadow_state.get("first_candidate_seconds_left"),
            "shadow_first_candidate_profile": shadow_state.get("first_candidate_profile", "none"),
            "shadow_first_candidate_score": shadow_state.get("first_candidate_score", 0.0),
            "shadow_first_live_decision": shadow_state.get("first_live_decision", "neutral"),
            "shadow_first_live_decision_progress_pct": shadow_state.get("first_live_decision_progress_pct"),
            "shadow_max_score": shadow_state.get("max_score", shadow.get("score", 0.0)),
            "shadow_max_score_profile": shadow_state.get("max_score_profile", shadow.get("profile", "none")),
            "shadow_max_live_decision": shadow_state.get("max_live_decision", shadow_live.get("decision", "neutral")),
            "core_ev_bucket_key": "",
            "core_ev_bucket_level": "",
            "core_ev_decision": "unknown",
            "core_ev_reason": "",
            "core_ev_sample_size": 0,
            "core_ev_historical_roi": 0.0,
            "core_ev_historical_win_rate": 0.0,
            "core_ev_recent_roi": 0.0,
            "core_ev_recent_trades": 0,
            "core_ev_size_fraction": 0.0,
        }

        shadow_live_decision = str(shadow_live.get("decision", "neutral") or "neutral")
        shadow_live_reason = str(shadow_live.get("reason", "") or "")
        shadow_live_score = float(shadow_live.get("score", 0) or 0)
        shadow_profile = str(shadow.get("profile", "none") or "none")
        shadow_live_mode = normalize_shadow_live_mode(SHADOW_LIVE_MODE)
        signal_data["shadow_live_mode"] = shadow_live_mode

        return {
            "signal_data": signal_data,
            "trade_amount": trade_amount,
            "market_prob": market_prob,
            "model_prob": model_prob,
            "edge": edge,
            "trend_aligned": trend_aligned,
            "trend_conflict": trend_conflict,
            "signal_tier": signal_tier,
            "signal_tier_reason": signal_tier_reason,
            "shadow_live": shadow_live,
            "shadow_live_decision": shadow_live_decision,
            "shadow_live_reason": shadow_live_reason,
            "shadow_live_score": shadow_live_score,
            "shadow_profile": shadow_profile,
            "shadow_live_mode": shadow_live_mode,
        }

    def _save_window_sample(self, snapshot: dict):
        sample = dict(snapshot.get("signal_data", {}))
        sample["record_type"] = "window_sample"
        sample["sample_source"] = "full_window_observe"
        save_window_sample(sample)

    def _candidate_priority(self, signal_data: dict, core_ev: dict, shadow_live_decision: str, shadow_live_score: float) -> tuple:
        decision = str(core_ev.get("decision", "unknown") or "unknown")
        decision_rank = {"strong_allow": 3, "allow": 2, "watch": 1, "deny": 0, "unknown": -1}.get(decision, -1)
        level_rank = {"L3": 3, "L2": 2, "L1": 1}.get(str(core_ev.get("bucket_level", "") or "").upper(), 0)
        shadow_rank = {"strong_allow": 3, "allow": 2, "watch": 1, "neutral": 0, "deny": -1}.get(shadow_live_decision, 0)
        return (
            decision_rank,
            level_rank,
            float(core_ev.get("historical_roi", 0) or 0),
            float(core_ev.get("recent_roi", 0) or 0),
            int(core_ev.get("sample_size", 0) or 0),
            shadow_rank,
            float(shadow_live_score or 0),
            float(signal_data.get("indicator_confirm", 0) or 0),
            float(signal_data.get("delta", 0) or 0),
        )

    def _should_take_full_window_entry(self, slug: str, signal_data: dict, core_ev: dict,
                                       shadow_live_decision: str, shadow_live_score: float,
                                       seconds_left: float) -> tuple[bool, str]:
        if not FULL_WINDOW_CORE_EV_ENABLED:
            return True, "full-window mode disabled"

        state = self.shadow_window_state.setdefault(slug, {"best_core_ev_candidate": None})
        candidate = {
            "priority": self._candidate_priority(signal_data, core_ev, shadow_live_decision, shadow_live_score),
            "time_left": float(seconds_left or 0),
            "confirm_ticks": int(state.get("observation_count", 0) or 0),
            "shadow_live_decision": shadow_live_decision,
            "shadow_live_score": float(shadow_live_score or 0),
            "historical_roi": float(core_ev.get("historical_roi", 0) or 0),
            "recent_roi": float(core_ev.get("recent_roi", 0) or 0),
            "sample_size": int(core_ev.get("sample_size", 0) or 0),
            "signal_score": float(signal_data.get("shadow_entry_score", 0) or 0),
        }

        best = state.get("best_core_ev_candidate")
        if best is None or candidate["priority"] > tuple(best.get("priority", ())):
            candidate["confirm_ticks"] = 1
            state["best_core_ev_candidate"] = candidate
            return False, "tracking new best full-window candidate"

        if candidate["priority"] == tuple(best.get("priority", ())) and abs(candidate["time_left"] - float(best.get("time_left", 0) or 0)) <= 6.0:
            best["confirm_ticks"] = int(best.get("confirm_ticks", 0) or 0) + 1
            state["best_core_ev_candidate"] = best
        else:
            if candidate["priority"][-2] >= best["priority"][-2] + FULL_WINDOW_ENTRY_MIN_SCORE_GAIN:
                candidate["confirm_ticks"] = 1
                state["best_core_ev_candidate"] = candidate
                return False, "tracking improved full-window candidate"

        best = state.get("best_core_ev_candidate")
        if best is None:
            return False, "waiting for full-window candidate"

        current_priority = candidate["priority"]
        best_priority = tuple(best.get("priority", ()))
        if current_priority != best_priority:
            return False, "current signal is not best in window"

        if int(best.get("confirm_ticks", 0) or 0) >= FULL_WINDOW_ENTRY_CONFIRM_TICKS:
            return True, f"best candidate confirmed for {int(best.get('confirm_ticks', 0) or 0)} ticks"

        if float(seconds_left or 0) <= FULL_WINDOW_ENTRY_COMMIT_TIME_LEFT:
            return True, f"commit threshold reached at {seconds_left:.1f}s"

        return False, "best candidate not confirmed yet"

    def _build_core_ev_bucket_keys(self, signal_data: dict) -> dict[str, str]:
        pm_bucket = bucket_pm_value(float(signal_data.get("pm", 0) or 0))
        delta_bucket = bucket_delta_value(float(signal_data.get("delta", 0) or 0))
        time_bucket = bucket_time_left_value(float(signal_data.get("time_left", 0) or 0))
        l1_pm_bucket = core_l1_pm_bucket_value(float(signal_data.get("pm", 0) or 0))
        l1_delta_bucket = core_l1_delta_bucket_value(float(signal_data.get("delta", 0) or 0))
        l1_time_bucket = core_l1_time_bucket_value(float(signal_data.get("time_left", 0) or 0))
        confirm_bucket = bucket_indicator_confirm_value(float(signal_data.get("indicator_confirm", 0) or 0))
        regime = str(signal_data.get("market_regime", "unknown") or "unknown")
        stable_bucket = bucket_stable_ticks_value(int(signal_data.get("stable_ticks", 0) or 0))
        profile = str(signal_data.get("shadow_entry_profile", "none") or "none")
        tier = str(signal_data.get("signal_tier", "unknown") or "unknown")
        trend_flag = "trend_ok" if bool(signal_data.get("trend_aligned")) and not bool(signal_data.get("trend_conflict")) else "trend_bad"
        return {
            "L1": " | ".join(["L1", f"pm:{l1_pm_bucket}", f"delta:{l1_delta_bucket}", f"time:{l1_time_bucket}", trend_flag]),
            "L2": " | ".join(["L2", f"pm:{pm_bucket}", f"delta:{delta_bucket}", f"time:{time_bucket}", f"regime:{regime}", f"stable:{stable_bucket}", f"tier:{tier}"]),
            "L3": " | ".join(["L3", f"pm:{pm_bucket}", f"delta:{delta_bucket}", f"time:{time_bucket}", f"confirm:{confirm_bucket}", f"regime:{regime}", f"stable:{stable_bucket}", f"profile:{profile}", f"tier:{tier}"]),
        }

    def _evaluate_core_ev_gate(self, signal_data: dict, shadow_live_decision: str) -> dict:
        if not CORE_EV_ENABLED:
            return {"decision": "allow", "reason": "core ev disabled", "size_fraction": 0.0}

        pm = float(signal_data.get("pm", 0) or 0)
        if pm < CORE_EV_PM_MIN or pm > CORE_EV_PM_MAX:
            return {"decision": "deny", "reason": f"pm outside core ev zone ({pm:.3f})", "size_fraction": 0.0}
        if not bool(signal_data.get("trend_aligned")) or bool(signal_data.get("trend_conflict")):
            return {"decision": "deny", "reason": "core ev requires aligned non-conflicting trend", "size_fraction": 0.0}
        if str(signal_data.get("signal_tier", "observe") or "observe") not in {"candidate", "trade"}:
            return {"decision": "deny", "reason": f"signal tier {signal_data.get('signal_tier', 'observe')} not core-eligible", "size_fraction": 0.0}
        if shadow_live_decision == "deny":
            return {"decision": "deny", "reason": "shadow live deny", "size_fraction": 0.0}
        if bool(signal_data.get("reversal_flag")) and not bool(signal_data.get("pullback_recovered")):
            return {"decision": "deny", "reason": "reversal risk not recovered", "size_fraction": 0.0}

        keys = self._build_core_ev_bucket_keys(signal_data)
        buckets = self.core_ev_rules.get("buckets", {}) if isinstance(self.core_ev_rules, dict) else {}
        selected_key = None
        selected_stats = None
        for level in ("L3", "L2", "L1"):
            key = keys[level]
            stats = buckets.get(key)
            if isinstance(stats, dict) and str(stats.get("decision", "unknown") or "unknown") != "unknown":
                selected_key = key
                selected_stats = stats
                break
        if selected_stats is None:
            return {
                "decision": "deny",
                "reason": "undersampled or unknown core ev bucket",
                "bucket_key": keys["L3"],
                "bucket_level": "L3",
                "sample_size": 0,
                "historical_roi": 0.0,
                "historical_win_rate": 0.0,
                "recent_roi": 0.0,
                "recent_trades": 0,
                "size_fraction": 0.0,
            }

        decision = str(selected_stats.get("decision", "deny") or "deny")
        bucket_level = str(selected_stats.get("level", "unknown") or "unknown")
        sample_size = int(selected_stats.get("trades", 0) or 0)
        historical_roi = float(selected_stats.get("roi", 0) or 0)
        historical_win_rate = float(selected_stats.get("win_rate", 0) or 0)
        recent_roi = float(selected_stats.get("recent_roi", 0) or 0)
        recent_trades = int(selected_stats.get("recent_trades", 0) or 0)
        time_left = float(signal_data.get("time_left", 0) or 0)
        min_level_rank = {"L1": 1, "L2": 2, "L3": 3}.get(FULL_WINDOW_CORE_EV_MIN_LEVEL, 2)
        bucket_level_rank = {"L1": 1, "L2": 2, "L3": 3}.get(bucket_level, 0)
        if FULL_WINDOW_CORE_EV_ENABLED and bucket_level_rank < min_level_rank:
            l1_fallback_ok = (
                bucket_level == "L1"
                and decision in {"allow", "strong_allow"}
                and sample_size >= FULL_WINDOW_L1_FALLBACK_MIN_TRADES
                and historical_roi > 0
                and time_left <= FULL_WINDOW_L1_FALLBACK_TIME_LEFT_MAX
                and (
                    not FULL_WINDOW_L1_FALLBACK_REQUIRE_RECENT_POSITIVE
                    or (recent_trades > 0 and recent_roi > 0)
                )
            )
            if l1_fallback_ok:
                size_fraction = CORE_EV_MAX_RISK_PCT if decision == "strong_allow" else CORE_EV_MAX_RISK_PCT * 0.5
                return {
                    "decision": decision,
                    "reason": (
                        f"core ev {decision} | full-window L1 fallback "
                        f"(sample={sample_size} roi={historical_roi:+.1f}% recent={recent_roi:+.1f}%)"
                    ),
                    "bucket_key": selected_key,
                    "bucket_level": bucket_level,
                    "sample_size": sample_size,
                    "historical_roi": historical_roi,
                    "historical_win_rate": historical_win_rate,
                    "recent_roi": recent_roi,
                    "recent_trades": recent_trades,
                    "size_fraction": size_fraction,
                }
            return {
                "decision": "deny",
                "reason": f"full-window requires {FULL_WINDOW_CORE_EV_MIN_LEVEL}+ bucket specificity",
                "bucket_key": selected_key,
                "bucket_level": bucket_level,
                "sample_size": sample_size,
                "historical_roi": historical_roi,
                "historical_win_rate": historical_win_rate,
                "recent_roi": recent_roi,
                "recent_trades": recent_trades,
                "size_fraction": 0.0,
            }

        size_fraction = 0.0
        if decision == "strong_allow":
            size_fraction = CORE_EV_MAX_RISK_PCT
        elif decision == "allow":
            size_fraction = CORE_EV_MAX_RISK_PCT * 0.5
        return {
            "decision": decision,
            "reason": f"core ev {decision}",
            "bucket_key": selected_key,
            "bucket_level": bucket_level,
            "sample_size": sample_size,
            "historical_roi": historical_roi,
            "historical_win_rate": historical_win_rate,
            "recent_roi": recent_roi,
            "recent_trades": recent_trades,
            "size_fraction": size_fraction,
        }

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
                self._finalize_window_summaries(close_ts)
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
                token_ids = list(market.get("clob_token_ids", []))
                outcomes = list(market.get("outcomes", []))
                prices = list(market.get("outcome_prices", []))
                if len(token_ids) >= 2 and len(outcomes) >= 2 and len(prices) >= 2:
                    midpoint_prices = []
                    for token_id in token_ids[:2]:
                        clob_price = get_clob_price(token_id)
                        midpoint_prices.append(clob_price if clob_price > 0 else 0.0)

                    refresh_count = sum(1 for price in midpoint_prices if price > 0)
                    use_midpoints = refresh_count >= 2
                    resolved_prices = midpoint_prices if use_midpoints else prices[:2]
                    winner_idx = 0 if resolved_prices[0] >= resolved_prices[1] else 1
                    market["outcome_prices"] = resolved_prices
                    market["winner_side"] = outcomes[winner_idx]
                    market["winner_price"] = resolved_prices[winner_idx]
                    market["winner_token"] = token_ids[winner_idx]
                    market["loser_price"] = resolved_prices[1 - winner_idx]
                    market["pm_price_spread"] = abs(resolved_prices[0] - resolved_prices[1])
                    market["pm_price_source"] = "clob_midpoint" if use_midpoints else "gamma"
                    market["clob_midpoint_refresh_count"] = refresh_count
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

                shadow_context = self._observe_shadow_window(market, ta, seconds_left)
                snapshot = self._build_signal_snapshot(market, ta, seconds_left, shadow_context)
                if WINDOW_SAMPLE_LOGGING_ENABLED:
                    self._save_window_sample(snapshot)

                # Log monitoring info when still outside entry window
                if seconds_left > ENTRY_SECONDS_MAX + 5:
                    log(f"   [{crypto}] {seconds_left:.0f}s | "
                        f"PM:{market['winner_side']}@{market['winner_price']:.3f} | "
                        f"Price:{ta.get('current_price',0):.2f} | "
                        f"delta:{ta.get('delta_pct',0):.4f}% | "
                        f"conf:{ta.get('confidence',0):.0%}")

                log(f"🎯 [{crypto}] {seconds_left:.1f}s | "
                    f"PM:{market['winner_side']}@{market['winner_price']:.3f} | "
                    f"Price:{ta.get('current_price',0):.2f} | "
                    f"delta:{ta.get('delta_pct',0):.4f}% | "
                    f"conf:{ta.get('confidence',0):.0%} | "
                    f"{ta.get('reason','')[:50]}")

                should_evaluate_entry = (
                    CORE_EV_ENTRY_TIME_MIN <= seconds_left <= CORE_EV_ENTRY_TIME_MAX
                    if FULL_WINDOW_CORE_EV_ENABLED
                    else ENTRY_SECONDS_MIN <= seconds_left <= ENTRY_SECONDS_MAX
                )
                if should_evaluate_entry:
                    self._evaluate_entry(
                        market,
                        ta,
                        seconds_left,
                        entered_slugs,
                        shadow_context,
                        snapshot=snapshot,
                        persist_signal=not FULL_WINDOW_CORE_EV_ENABLED,
                    )

            time.sleep(POLL_INTERVAL)

    def _evaluate_entry(self, market, ta, seconds_left, entered_slugs, shadow_context: dict | None = None,
                        snapshot: dict | None = None, persist_signal: bool = True):
        snapshot = snapshot or self._build_signal_snapshot(market, ta, seconds_left, shadow_context)
        signal_data = snapshot["signal_data"]
        slug      = market["slug"]
        crypto    = market["crypto"]
        price_min = PRICE_MIN.get(crypto, 0.92)
        trade_amount = float(snapshot.get("trade_amount", signal_data.get("amount", self.amount)) or self.amount)
        market_prob = float(snapshot.get("market_prob", signal_data.get("market_prob", 0)) or 0)
        model_prob = float(snapshot.get("model_prob", signal_data.get("model_prob", 0)) or 0)
        edge = float(snapshot.get("edge", signal_data.get("edge", 0)) or 0)
        trend_aligned = bool(snapshot.get("trend_aligned", signal_data.get("trend_aligned")))
        trend_conflict = bool(snapshot.get("trend_conflict", signal_data.get("trend_conflict")))
        signal_tier = str(snapshot.get("signal_tier", signal_data.get("signal_tier", "observe")) or "observe")
        signal_tier_reason = str(snapshot.get("signal_tier_reason", signal_data.get("signal_tier_reason", "")) or "")
        shadow_live = snapshot.get("shadow_live", {})
        shadow_live_decision = str(snapshot.get("shadow_live_decision", signal_data.get("shadow_live_decision", "neutral")) or "neutral")
        shadow_live_reason = str(snapshot.get("shadow_live_reason", signal_data.get("shadow_live_reason", "")) or "")
        shadow_live_score = float(snapshot.get("shadow_live_score", signal_data.get("shadow_live_score", 0)) or 0)
        shadow_profile = str(snapshot.get("shadow_profile", signal_data.get("shadow_entry_profile", "none")) or "none")
        shadow_live_mode = str(snapshot.get("shadow_live_mode", signal_data.get("shadow_live_mode", normalize_shadow_live_mode(SHADOW_LIVE_MODE))) or normalize_shadow_live_mode(SHADOW_LIVE_MODE))

        def persist_record(force: bool = False):
            if persist_signal or force:
                save_signal(signal_data)
        if shadow_live_mode != "off" and shadow_live_decision in {"strong_allow", "allow", "watch", "deny"}:
            log(
                f"   [{crypto}] SHADOW-{shadow_live_decision.upper()} — "
                f"profile={shadow_profile} score={shadow_live_score:.2f} "
                f"regime={shadow_live.get('market_regime', 'unknown')} "
                f"stable={shadow_live.get('stable_ticks', 0)} streak={shadow_live.get('recent_5m_streak', 0)} "
                f"progress={float(shadow_live.get('window_progress_pct', 0) or 0) * 100:.0f}% "
                f"gap={float(shadow_live.get('pm_vs_delta_gap', 0) or 0):+.3f} "
                f"underpricing={float(shadow_live.get('underpricing_score', 0) or 0):+.3f} | "
                f"{shadow_live_reason}"
            )

        shadow_live_blocks = shadow_live_mode in {"block_deny", "hybrid"} and shadow_live_decision == "deny"
        shadow_live_relax_filters = shadow_live_mode == "hybrid" and shadow_live_decision in {"allow", "strong_allow"}

        if self._daily_loss_limit_hit():
            log(f"   [{crypto}] SKIP — daily loss limit reached (${self.daily_loss_limit:.2f})")
            signal_data["reason"] = f"daily loss limit reached ({self.daily_loss_limit:.2f})"
            persist_record()
            return

        if shadow_live_blocks:
            log(f"   [{crypto}] SKIP — shadow live deny ({shadow_live_reason})")
            signal_data["reason"] = f"shadow live deny | {shadow_live_reason}"
            persist_record()
            return

        confidence = float(ta.get("confidence", 0) or 0)
        indicator_confirm = float(ta.get("indicator_confirm", 0) or 0)
        delta_pct = float(ta.get("delta_pct", 0) or 0)
        pm_price = float(market["winner_price"] or 0)
        core_first_mode = CORE_EV_ENABLED

        # Filter 1: legacy PM bounds. In Core EV mode these become diagnostic only;
        # the Core EV rulebook remains the actual entry gate.
        if pm_price < price_min:
            if core_first_mode:
                log(f"   [{crypto}] NOTE — legacy PM floor {pm_price:.3f} < {price_min} ignored in core-first mode")
            else:
                log(f"   [{crypto}] SKIP — PM price {market['winner_price']:.3f} < {price_min}")
                signal_data["reason"] = f"PM price < {price_min}"
                persist_record()
                return

        # Filter 1b: legacy maximum price / overprice override. In Core EV mode this
        # is informational only because Core EV already enforces its own PM zone.
        strong_overprice_ok = (
            pm_price <= PRICE_MAX_STRONG
            and delta_pct >= STRONG_OVERPRICE_DELTA_MIN
            and confidence >= STRONG_OVERPRICE_CONFIDENCE_MIN
            and indicator_confirm >= STRONG_OVERPRICE_INDICATOR_CONFIRM_MIN
            and edge >= STRONG_OVERPRICE_EDGE_MIN
            and trend_aligned
            and seconds_left >= STRONG_OVERPRICE_TIME_LEFT_MIN
        )
        if pm_price > PRICE_MAX and not strong_overprice_ok:
            ceiling = PRICE_MAX_STRONG if pm_price <= PRICE_MAX_STRONG else PRICE_MAX
            if pm_price > PRICE_MAX_STRONG:
                override_reason = f"pm above strong max {PRICE_MAX_STRONG:.2f}"
            elif delta_pct < STRONG_OVERPRICE_DELTA_MIN:
                override_reason = f"delta {delta_pct:.4f}% < {STRONG_OVERPRICE_DELTA_MIN:.3f}%"
            elif confidence < STRONG_OVERPRICE_CONFIDENCE_MIN:
                override_reason = f"confidence {confidence:.0%} < {STRONG_OVERPRICE_CONFIDENCE_MIN:.0%}"
            elif indicator_confirm < STRONG_OVERPRICE_INDICATOR_CONFIRM_MIN:
                override_reason = (
                    f"1m confirm {indicator_confirm:+.2f} < {STRONG_OVERPRICE_INDICATOR_CONFIRM_MIN:+.2f}"
                )
            elif edge < STRONG_OVERPRICE_EDGE_MIN:
                override_reason = f"edge {edge:+.3f} < {STRONG_OVERPRICE_EDGE_MIN:+.3f}"
            elif not trend_aligned:
                override_reason = "trend not aligned"
            elif seconds_left < STRONG_OVERPRICE_TIME_LEFT_MIN:
                override_reason = f"time_left {seconds_left:.1f}s < {STRONG_OVERPRICE_TIME_LEFT_MIN:.0f}s"
            else:
                override_reason = "strong override failed"
            log(
                f'   [{crypto}] {"NOTE" if core_first_mode else "SKIP"} — PM price {pm_price:.3f} > {PRICE_MAX} '
                f'({override_reason}; '
                f'(minimal upside; strong override requires delta>={STRONG_OVERPRICE_DELTA_MIN:.3f}% '
                f'conf>={STRONG_OVERPRICE_CONFIDENCE_MIN:.0%} edge>={STRONG_OVERPRICE_EDGE_MIN:+.3f} '
                f'1m>={STRONG_OVERPRICE_INDICATOR_CONFIRM_MIN:+.2f} '
                f'time_left>={STRONG_OVERPRICE_TIME_LEFT_MIN:.0f}s trend_aligned)'
            )
            if not core_first_mode:
                signal_data["reason"] = f"PM price > {ceiling:.2f} | {override_reason}"
                persist_record()
                return
        if pm_price > PRICE_MAX:
            log(
                f"   [{crypto}] ALLOW — PM price {pm_price:.3f} above base max {PRICE_MAX} "
                f"but strong setup override passed"
            )

        normal_zone_live_pass = (
            pm_price >= NORMAL_ZONE_MIN
            and pm_price <= NORMAL_ZONE_MAX
            and delta_pct >= NORMAL_ZONE_DELTA_MIN
            and indicator_confirm >= NORMAL_ZONE_INDICATOR_CONFIRM_MIN
            and edge >= NORMAL_ZONE_EDGE_MIN
            and trend_aligned
            and seconds_left >= NORMAL_ZONE_TIME_LEFT_MIN
        )
        if normal_zone_live_pass:
            log(
                f"   [{crypto}] ALLOW — normal zone live pass "
                f"(pm={pm_price:.3f} delta={delta_pct:.4f}% edge={edge:+.3f} "
                f"1m={indicator_confirm:+.2f} time_left={seconds_left:.1f}s)"
            )

        hybrid_shadow_live_pass = (
            shadow_live_relax_filters
            and not normal_zone_live_pass
            and pm_price >= HYBRID_SHADOW_PM_MIN
        )
        if hybrid_shadow_live_pass:
            log(
                f"   [{crypto}] ALLOW — hybrid shadow live pass "
                f"(decision={shadow_live_decision} profile={shadow_profile} score={shadow_live_score:.2f} "
                f"pm={pm_price:.3f} delta={delta_pct:.4f}% edge={edge:+.3f} 1m={indicator_confirm:+.2f} "
                f"time_left={seconds_left:.1f}s)"
            )
        elif shadow_live_relax_filters and not normal_zone_live_pass and pm_price < HYBRID_SHADOW_PM_MIN:
            log(
                f"   [{crypto}] SHADOW-ALLOW BLOCKED — hybrid shadow PM too cheap "
                f"(pm={pm_price:.3f} < {HYBRID_SHADOW_PM_MIN:.2f}; decision={shadow_live_decision} "
                f"profile={shadow_profile} score={shadow_live_score:.2f})"
            )

        # Legacy confidence/edge/1m filters become advisory in Core EV mode.
        if confidence < MIN_CONFIDENCE and not normal_zone_live_pass and not hybrid_shadow_live_pass:
            if core_first_mode:
                log(f"   [{crypto}] NOTE — legacy confidence gate {confidence:.0%} < {MIN_CONFIDENCE:.0%} ignored in core-first mode")
            else:
                log(f"   [{crypto}] SKIP — confidence {confidence:.0%} < {MIN_CONFIDENCE:.0%}")
                signal_data["reason"] = f"confidence < {MIN_CONFIDENCE:.0%}"
                persist_record()
                return

        if edge < MIN_EDGE and not normal_zone_live_pass and not hybrid_shadow_live_pass:
            if core_first_mode:
                log(
                    f"   [{crypto}] NOTE — legacy edge gate {edge:+.3f} < {MIN_EDGE:+.3f} "
                    f"(model={model_prob:.3f} market={market_prob:.3f}) ignored in core-first mode"
                )
            else:
                log(
                    f"   [{crypto}] SKIP — edge {edge:+.3f} < {MIN_EDGE:+.3f} "
                    f"(model={model_prob:.3f} market={market_prob:.3f})"
                )
                signal_data["reason"] = f"edge {edge:+.3f} < {MIN_EDGE:+.3f}"
                persist_record()
                return

        if indicator_confirm < INDICATOR_CONFIRM_MIN and not normal_zone_live_pass and not hybrid_shadow_live_pass:
            if core_first_mode:
                log(
                    f"   [{crypto}] NOTE — legacy 1m gate {indicator_confirm:+.2f} < {INDICATOR_CONFIRM_MIN:+.2f} "
                    f"({ta.get('indicator_reason', 'neutral')}) ignored in core-first mode"
                )
            else:
                log(
                    f"   [{crypto}] SKIP — 1m confirm {indicator_confirm:+.2f} < {INDICATOR_CONFIRM_MIN:+.2f} "
                    f"({ta.get('indicator_reason', 'neutral')})"
                )
                signal_data["reason"] = f"1m confirm {indicator_confirm:+.2f} < {INDICATOR_CONFIRM_MIN:+.2f}"
                persist_record()
                return

        # Filter 3: direction must match between Binance and Polymarket
        ta_dir  = ta.get("direction")
        pm_side = market["winner_side"]
        if ta_dir and ta_dir != pm_side:
            spread = float(market.get("pm_price_spread", 0) or 0)
            source = str(market.get("pm_price_source", "gamma") or "gamma")
            refresh_count = int(market.get("clob_midpoint_refresh_count", 0) or 0)
            log(
                f"   [{crypto}] SKIP — Binance says {ta_dir} but PM says {pm_side} "
                f"(pm={pm_price:.3f} spread={spread:.3f} source={source} clob_refresh={refresh_count}/2)"
            )
            signal_data["reason"] = (
                f"direction mismatch: {ta_dir} vs {pm_side} "
                f"(spread={spread:.3f} source={source} clob_refresh={refresh_count}/2)"
            )
            persist_record()
            return

        # Legacy delta/tier filters also become advisory in Core EV mode. Core EV
        # itself will reject weak signals through its own bucket/tier logic.
        if delta_pct < DELTA_SKIP * 100:
            if core_first_mode:
                log(f"   [{crypto}] NOTE — legacy delta gate {delta_pct:.4f}% too small ignored in core-first mode")
            else:
                log(f"   [{crypto}] SKIP — delta {delta_pct:.4f}% too small")
                signal_data["reason"] = f"delta {delta_pct:.4f}% too small"
                persist_record()
                return

        if signal_tier == "observe" and not normal_zone_live_pass and not hybrid_shadow_live_pass:
            if core_first_mode:
                log(f"   [{crypto}] NOTE — legacy signal tier observe ({signal_tier_reason}) ignored in core-first mode")
            else:
                log(f"   [{crypto}] SKIP — signal tier observe ({signal_tier_reason})")
                signal_data["reason"] = f"signal tier observe | {signal_tier_reason}"
                persist_record()
                return

        core_ev = self._evaluate_core_ev_gate(signal_data, shadow_live_decision)
        signal_data["core_ev_bucket_key"] = str(core_ev.get("bucket_key", "") or "")
        signal_data["core_ev_bucket_level"] = str(core_ev.get("bucket_level", "") or "")
        signal_data["core_ev_decision"] = str(core_ev.get("decision", "unknown") or "unknown")
        signal_data["core_ev_reason"] = str(core_ev.get("reason", "") or "")
        signal_data["core_ev_sample_size"] = int(core_ev.get("sample_size", 0) or 0)
        signal_data["core_ev_historical_roi"] = float(core_ev.get("historical_roi", 0) or 0)
        signal_data["core_ev_historical_win_rate"] = float(core_ev.get("historical_win_rate", 0) or 0)
        signal_data["core_ev_recent_roi"] = float(core_ev.get("recent_roi", 0) or 0)
        signal_data["core_ev_recent_trades"] = int(core_ev.get("recent_trades", 0) or 0)
        signal_data["core_ev_size_fraction"] = float(core_ev.get("size_fraction", 0) or 0)

        core_ev_decision = str(core_ev.get("decision", "unknown") or "unknown")
        if CORE_EV_ENABLED:
            log(
                f"   [{crypto}] CORE-EV {core_ev_decision.upper()} — "
                f"level={signal_data['core_ev_bucket_level']} sample={signal_data['core_ev_sample_size']} "
                f"roi={signal_data['core_ev_historical_roi']:+.1f}% recent={signal_data['core_ev_recent_roi']:+.1f}% | "
                f"{signal_data['core_ev_reason']}"
            )
        if core_ev_decision not in {"allow", "strong_allow"}:
            signal_data["reason"] = f"core ev {core_ev_decision} | {signal_data['core_ev_reason']}"
            persist_record()
            return

        allow_full_window_entry, full_window_reason = self._should_take_full_window_entry(
            slug,
            signal_data,
            core_ev,
            shadow_live_decision,
            shadow_live_score,
            seconds_left,
        )
        signal_data["full_window_entry_reason"] = full_window_reason
        if FULL_WINDOW_CORE_EV_ENABLED and not allow_full_window_entry:
            log(f"   [{crypto}] WAIT — {full_window_reason}")
            signal_data["reason"] = f"full window wait | {full_window_reason}"
            return

        if not FULL_WINDOW_CORE_EV_ENABLED and CORE_EV_ENABLED and not (CORE_EV_TIME_LEFT_MIN <= seconds_left < CORE_EV_TIME_LEFT_MAX):
            log(
                f"   [{crypto}] SKIP — core ev timing policy requires "
                f"{CORE_EV_TIME_LEFT_MIN:.0f}-{CORE_EV_TIME_LEFT_MAX:.0f}s exclusive upper bound "
                f"(got {seconds_left:.1f}s)"
            )
            signal_data["reason"] = (
                f"core ev timing policy | time_left {seconds_left:.1f}s outside "
                f"{CORE_EV_TIME_LEFT_MIN:.0f}-{CORE_EV_TIME_LEFT_MAX:.0f}s"
            )
            persist_record()
            return

        core_ev_size_fraction = float(core_ev.get("size_fraction", 0) or 0)
        if core_ev_size_fraction > 0:
            core_ev_amount = self.bank_balance * core_ev_size_fraction
            trade_amount = max(self.dynamic_min_amount, min(core_ev_amount, self.dynamic_max_amount, self.bank_balance))
            signal_data["amount"] = trade_amount

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
                persist_record(force=True)
                return

        # Entry approved. Mark it as entered only after a successful execution.
        expected_pnl = (trade_amount / market["winner_price"]) - trade_amount
        signal_data["pnl_expected"] = expected_pnl

        execution = self._enter(
            market,
            ta,
            seconds_left,
            trade_amount,
            signal_tier,
            signal_tier_reason,
        )
        if execution.get("ok"):
            signal_data["entered"] = True
            signal_data["reason"] = "all filters passed"
            entered_slugs.add(slug)
            self.traded_slugs.add(slug)
            state = self.shadow_window_state.get(slug)
            if isinstance(state, dict):
                state["best_core_ev_candidate"] = None
        else:
            signal_data["entered"] = False
            signal_data["reason"] = "execution failed"
            signal_data["execution_failure_type"] = str(execution.get("failure_type", "") or "unknown")
            signal_data["execution_failure_detail"] = str(execution.get("detail", "") or "")
            signal_data["execution_order_status"] = str(execution.get("order_status", "") or "")
            signal_data["execution_order_id"] = str(execution.get("order_id", "") or "")
            if execution.get("size") is not None:
                signal_data["execution_size"] = execution.get("size")
            if execution.get("taker_price") is not None:
                signal_data["execution_taker_price"] = execution.get("taker_price")

        persist_record(force=True)

    def _observe_shadow_window(self, market: dict, ta: dict, seconds_left: float, allow_log: bool = True) -> dict:
        slug = str(market.get("slug") or "")
        history = self._record_window_tick(market, ta, seconds_left)
        features = self._build_window_features(history, market, ta, seconds_left)
        shadow = self._evaluate_shadow_entry(features, market, ta, seconds_left)
        shadow_live = self._evaluate_shadow_live_decision(features, shadow, market, ta)

        state = self.shadow_window_state.get(slug)
        if state is None:
            state = {
                "observation_count": 0,
                "first_progress_pct": float(features.get("window_progress_pct", 0.0) or 0.0),
                "first_candidate_progress_pct": None,
                "first_candidate_seconds_left": None,
                "first_candidate_profile": "none",
                "first_candidate_score": 0.0,
                "first_live_decision": "neutral",
                "first_live_decision_progress_pct": None,
                "max_score": 0.0,
                "max_score_profile": "none",
                "max_live_decision": "neutral",
                "best_core_ev_candidate": None,
                "last_logged_key": None,
            }
            self.shadow_window_state[slug] = state

        state["observation_count"] = int(state.get("observation_count", 0) or 0) + 1

        if shadow.get("candidate") and state.get("first_candidate_progress_pct") is None:
            state["first_candidate_progress_pct"] = float(features.get("window_progress_pct", 0.0) or 0.0)
            state["first_candidate_seconds_left"] = float(seconds_left or 0)
            state["first_candidate_profile"] = str(shadow.get("profile", "none") or "none")
            state["first_candidate_score"] = float(shadow.get("score", 0.0) or 0.0)

        if state.get("first_live_decision_progress_pct") is None and shadow_live.get("decision") in {"watch", "allow", "strong_allow", "deny"}:
            state["first_live_decision"] = str(shadow_live.get("decision", "neutral") or "neutral")
            state["first_live_decision_progress_pct"] = float(features.get("window_progress_pct", 0.0) or 0.0)

        shadow_score = float(shadow.get("score", 0.0) or 0.0)
        if shadow_score >= float(state.get("max_score", 0.0) or 0.0):
            state["max_score"] = shadow_score
            state["max_score_profile"] = str(shadow.get("profile", "none") or "none")

        live_decision = str(shadow_live.get("decision", "neutral") or "neutral")
        decision_rank = {"neutral": 0, "watch": 1, "allow": 2, "strong_allow": 3}.get(live_decision, -1)
        best_rank = {"neutral": 0, "watch": 1, "allow": 2, "strong_allow": 3}.get(str(state.get("max_live_decision", "neutral") or "neutral"), -1)
        if decision_rank >= best_rank:
            state["max_live_decision"] = live_decision

        if allow_log and seconds_left > ENTRY_SECONDS_MAX:
            profile = str(shadow.get("profile", "none") or "none")
            progress_pct = float(features.get("window_progress_pct", 0.0) or 0.0) * 100.0
            log_key = (live_decision, profile, str(shadow_live.get("reason", "") or shadow.get("reason", "")))
            if live_decision in {"watch", "allow", "strong_allow", "deny"} and state.get("last_logged_key") != log_key:
                log(
                    f"   [{market.get('crypto', '?')}] SHADOW-OBSERVE {live_decision.upper()} — "
                    f"profile={profile} score={shadow_score:.2f} "
                    f"progress={progress_pct:.0f}% stable={features.get('stable_ticks', 0)} "
                    f"streak={features.get('recent_5m_streak', 0)} "
                    f"gap={float(features.get('pm_vs_delta_gap', 0) or 0):+.3f} "
                    f"underpricing={float(features.get('underpricing_score', 0) or 0):+.3f} | "
                    f"{shadow_live.get('reason', shadow.get('reason', ''))}"
                )
                state["last_logged_key"] = log_key

        return {
            "history": history,
            "features": features,
            "shadow": shadow,
            "shadow_live": shadow_live,
        }

    def _record_window_tick(self, market: dict, ta: dict, seconds_left: float) -> deque:
        slug = str(market.get("slug") or "")
        history = self.window_history.get(slug)
        if history is None:
            history = deque(maxlen=WINDOW_HISTORY_MAX_POINTS)
            self.window_history[slug] = history

        history.append({
            "ts": now_unix(),
            "seconds_left": float(seconds_left or 0),
            "pm_price": float(market.get("winner_price", 0) or 0),
            "binance_price": float(ta.get("current_price", 0) or 0),
            "delta_pct": float(ta.get("delta_pct", 0) or 0),
            "direction": ta.get("direction") or "none",
            "indicator_confirm": float(ta.get("indicator_confirm", 0) or 0),
            "trend_aligned": bool(ta.get("trend_aligned")),
            "higher_trend": ta.get("higher_trend") or "unknown",
        })
        return history

    def _build_window_features(self, history: deque, market: dict, ta: dict, seconds_left: float) -> dict:
        points = list(history)
        if not points:
            return {}

        current = points[-1]
        directions = [p.get("direction") for p in points if p.get("direction") in ("Up", "Down")]
        stable_ticks = 0
        if directions:
            last_direction = directions[-1]
            for direction in reversed(directions):
                if direction == last_direction:
                    stable_ticks += 1
                else:
                    break
        direction_persistence = stable_ticks / max(len(points), 1)

        delta_values = [float(p.get("delta_pct", 0) or 0) for p in points]
        pm_values = [float(p.get("pm_price", 0) or 0) for p in points]
        delta_slope = delta_values[-1] - delta_values[0] if len(delta_values) >= 2 else 0.0
        pm_slope = pm_values[-1] - pm_values[0] if len(pm_values) >= 2 else 0.0
        max_delta = max(delta_values) if delta_values else 0.0
        min_delta = min(delta_values) if delta_values else 0.0
        pullback_size = max_delta - delta_values[-1] if delta_values and delta_values[-1] >= 0 else abs(min_delta - delta_values[-1])
        pullback_recovered = len(delta_values) >= 3 and delta_values[-1] >= delta_values[-2] and delta_values[-1] >= delta_values[0]
        reversal_flag = len(delta_values) >= 3 and ((delta_values[-1] > 0 > min_delta) or (delta_values[-1] < 0 < max_delta))
        window_progress_pct = clamp((300.0 - float(seconds_left or 0)) / 300.0, 0.0, 1.0)
        pm_vs_delta_gap = float(current.get("pm_price", 0) or 0) - clamp(abs(delta_values[-1]) * 20.0, 0.0, 0.99)
        underpricing_score = clamp(abs(delta_values[-1]) * 15.0 - float(current.get("pm_price", 0) or 0), -1.0, 1.0)

        recent_streak = 0
        regime = "unknown"
        if self.closed_window_summaries:
            last_dir = self.closed_window_summaries[-1].get("direction")
            if last_dir in ("Up", "Down"):
                for item in reversed(self.closed_window_summaries):
                    if item.get("direction") == last_dir:
                        recent_streak += 1
                    else:
                        break
            if recent_streak >= 2:
                regime = f"trend_{str(last_dir).lower()}"
            elif len(self.closed_window_summaries) >= 4:
                dirs = [item.get("direction") for item in list(self.closed_window_summaries)[-4:]]
                unique_dirs = {d for d in dirs if d in ("Up", "Down")}
                regime = "chop" if len(unique_dirs) > 1 else regime

        return {
            "stable_ticks": stable_ticks,
            "direction_persistence": direction_persistence,
            "delta_slope": delta_slope,
            "pm_slope": pm_slope,
            "pullback_size": pullback_size,
            "pullback_recovered": pullback_recovered,
            "reversal_flag": reversal_flag,
            "window_progress_pct": window_progress_pct,
            "recent_5m_streak": recent_streak,
            "market_regime": regime,
            "pm_vs_delta_gap": pm_vs_delta_gap,
            "underpricing_score": underpricing_score,
        }

    def _evaluate_shadow_entry(self, features: dict, market: dict, ta: dict, seconds_left: float) -> dict:
        pm_price = float(market.get("winner_price", 0) or 0)
        delta_pct = float(ta.get("delta_pct", 0) or 0)
        trend_aligned = bool(ta.get("trend_aligned"))
        indicator_confirm = float(ta.get("indicator_confirm", 0) or 0)
        stable_ticks = int(features.get("stable_ticks", 0) or 0)
        direction_persistence = float(features.get("direction_persistence", 0) or 0)
        regime = str(features.get("market_regime", "unknown") or "unknown")
        recent_streak = int(features.get("recent_5m_streak", 0) or 0)
        progress = float(features.get("window_progress_pct", 0) or 0)
        underpricing_score = float(features.get("underpricing_score", 0) or 0)
        pm_gap = float(features.get("pm_vs_delta_gap", 0) or 0)
        pullback_size = float(features.get("pullback_size", 0) or 0)
        pullback_recovered = bool(features.get("pullback_recovered"))
        reversal_flag = bool(features.get("reversal_flag"))
        regime_support = regime.startswith("trend_") and recent_streak >= 2
        soft_structure_ok = stable_ticks >= SHADOW_SOFT_STABLE_TICKS or direction_persistence >= 0.45
        early_shadow_too_cheap = pm_price < SHADOW_EARLY_PM_FLOOR and seconds_left >= 45

        if not trend_aligned and not regime_support:
            return {"candidate": False, "profile": "none", "score": 0.0, "reason": "trend not aligned"}
        if pm_price > SHADOW_PM_MAX:
            return {"candidate": False, "profile": "none", "score": 0.0, "reason": f"pm too expensive for shadow ({pm_price:.3f})"}
        if reversal_flag and not pullback_recovered:
            return {"candidate": False, "profile": "none", "score": 0.0, "reason": "reversal not recovered"}
        if early_shadow_too_cheap:
            return {"candidate": False, "profile": "none", "score": 0.0, "reason": f"pm too cheap for early shadow ({pm_price:.3f})"}

        profile = "none"
        score = 0.0
        reason = ""
        if not soft_structure_ok:
            if regime_support and pm_price <= SHADOW_PROBE_PM_MAX and delta_pct >= SHADOW_PROBE_DELTA_MIN and underpricing_score >= 0:
                profile = "trend_regime_probe"
                score = stable_ticks * 0.6 + direction_persistence * 2.5 + delta_pct * 75 + max(0.0, underpricing_score) * 4
                reason = "regime support with forming structure"
            else:
                return {"candidate": False, "profile": "none", "score": 0.0, "reason": "not enough stable ticks"}
        elif 75 <= seconds_left <= 210 and delta_pct >= SHADOW_PROBE_DELTA_MIN and underpricing_score >= 0 and pm_price <= SHADOW_PROBE_PM_MAX:
            profile = "trend_regime_probe"
            score = stable_ticks * 0.7 + direction_persistence * 2.0 + delta_pct * 70 + max(0.0, underpricing_score) * 4
            reason = "forming trend with acceptable PM lag"
        elif 90 <= seconds_left <= 180 and delta_pct >= SHADOW_EARLY_DELTA_MIN and underpricing_score >= SHADOW_UNDERPRICING_MIN:
            profile = "trend_early"
            score = stable_ticks * 0.8 + delta_pct * 80 + underpricing_score * 5
            reason = "stable early trend with PM lag"
        elif 45 <= seconds_left <= 150 and pullback_size <= SHADOW_PULLBACK_MAX and pullback_recovered and delta_pct >= SHADOW_EARLY_DELTA_MIN:
            if pm_price < SHADOW_OBSERVE_PM_FLOOR:
                return {"candidate": False, "profile": "none", "score": 0.0, "reason": f"pm too cheap for pullback shadow ({pm_price:.3f})"}
            profile = "trend_pullback_resume"
            score = stable_ticks * 0.7 + delta_pct * 70 + max(0.0, 0.02 - pullback_size) * 100
            reason = "pullback recovered into trend"
        elif 12 <= seconds_left <= 45 and delta_pct >= SHADOW_LATE_DELTA_MIN and indicator_confirm >= -0.05:
            if pm_price < SHADOW_OBSERVE_PM_FLOOR:
                return {"candidate": False, "profile": "none", "score": 0.0, "reason": f"pm too cheap for late shadow ({pm_price:.3f})"}
            profile = "late_lock"
            score = stable_ticks * 0.6 + delta_pct * 90 + underpricing_score * 3
            reason = "late lock with sustained move"
        elif regime_support and progress >= 0.55 and delta_pct >= SHADOW_PROBE_DELTA_MIN and pm_gap <= 0.10 and pm_price <= SHADOW_PM_MAX:
            profile = "trend_regime_probe"
            score = stable_ticks * 0.6 + direction_persistence * 2.0 + delta_pct * 65 + max(0.0, underpricing_score) * 3
            reason = "regime continuation with acceptable late pricing"

        return {
            "candidate": profile != "none",
            "profile": profile,
            "score": round(score, 4),
            "reason": reason or "no shadow profile matched",
        }

    def _evaluate_shadow_live_decision(self, features: dict, shadow: dict, market: dict, ta: dict) -> dict:
        profile = str(shadow.get("profile", "none") or "none")
        score = float(shadow.get("score", 0) or 0)
        candidate = bool(shadow.get("candidate"))
        pm_price = float(market.get("winner_price", 0) or 0)
        delta_pct = float(ta.get("delta_pct", 0) or 0)
        trend_aligned = bool(ta.get("trend_aligned"))
        stable_ticks = int(features.get("stable_ticks", 0) or 0)
        recent_streak = int(features.get("recent_5m_streak", 0) or 0)
        regime = str(features.get("market_regime", "unknown") or "unknown")
        progress = float(features.get("window_progress_pct", 0) or 0)
        underpricing_score = float(features.get("underpricing_score", 0) or 0)
        pm_gap = float(features.get("pm_vs_delta_gap", 0) or 0)
        pullback_recovered = bool(features.get("pullback_recovered"))
        reversal_flag = bool(features.get("reversal_flag"))
        direction_persistence = float(features.get("direction_persistence", 0) or 0)

        decision = "neutral"
        reason = "no live shadow edge"
        regime_support = regime.startswith("trend_") and recent_streak >= 2
        early_shadow_too_cheap = pm_price < SHADOW_OBSERVE_PM_FLOOR and progress < SHADOW_OBSERVE_CHEAP_PM_MAX_PROGRESS

        if early_shadow_too_cheap and candidate:
            decision = "neutral"
            reason = f"pm too cheap for reliable early shadow ({pm_price:.3f})"
            candidate = False
            profile = "none"
            score = 0.0
            return {
                "decision": decision,
                "reason": reason,
                "profile": profile,
                "score": round(score, 4),
                "candidate": candidate,
                "market_regime": regime,
                "stable_ticks": stable_ticks,
                "recent_5m_streak": recent_streak,
                "window_progress_pct": round(progress, 4),
                "underpricing_score": round(underpricing_score, 4),
                "pm_vs_delta_gap": round(pm_gap, 4),
            }

        if not candidate:
            if not trend_aligned:
                if (
                    regime_support
                    and underpricing_score >= SHADOW_REGIME_SUPPORT_UNDERPRICING_MIN
                    and SHADOW_OBSERVE_PM_FLOOR <= pm_price <= SHADOW_PROBE_PM_MAX
                    and progress >= 0.75
                ):
                    decision = "watch"
                    reason = "regime support offsets local trend misalignment"
                else:
                    decision = "deny"
                    reason = "trend not aligned"
            elif stable_ticks < SHADOW_MIN_STABLE_TICKS and direction_persistence < 0.45:
                decision = "neutral"
                reason = "waiting for stable window structure"
            elif (
                regime_support
                and SHADOW_OBSERVE_PM_FLOOR <= pm_price <= SHADOW_PROBE_PM_MAX
                and delta_pct >= SHADOW_PROBE_DELTA_MIN
                and underpricing_score >= 0
                and progress >= 0.75
            ):
                decision = "watch"
                reason = "forming structure with regime support"
            else:
                reason = str(shadow.get("reason", "no shadow candidate") or "no shadow candidate")
        else:
            if reversal_flag and not pullback_recovered:
                decision = "deny"
                reason = "reversal risk not recovered"
            elif pm_gap >= SHADOW_LIVE_DENY_MAX_PM_GAP and progress >= SHADOW_LIVE_DENY_MIN_PROGRESS:
                decision = "deny"
                reason = "pm already too far ahead late in window"
            elif profile == "trend_regime_probe" and regime_support and score >= SHADOW_LIVE_ALLOW_MIN_SCORE and pm_price <= SHADOW_PROBE_PM_MAX:
                decision = "allow"
                reason = "regime probe aligned with broader 5m continuation"
            elif profile == "trend_regime_probe" and score >= SHADOW_LIVE_WATCH_MIN_SCORE:
                decision = "watch"
                reason = "probe candidate worth monitoring for continuation"
            elif profile == "trend_early" and score >= SHADOW_LIVE_STRONG_ALLOW_MIN_SCORE and underpricing_score >= SHADOW_UNDERPRICING_MIN and recent_streak >= 2:
                decision = "strong_allow"
                reason = "trend early with regime continuation and PM lag"
            elif profile == "trend_pullback_resume" and score >= SHADOW_LIVE_ALLOW_MIN_SCORE and pullback_recovered and stable_ticks >= SHADOW_MIN_STABLE_TICKS:
                decision = "allow"
                reason = "pullback resumed into stable aligned trend"
            elif profile == "late_lock" and score >= SHADOW_LIVE_ALLOW_MIN_SCORE and regime.startswith("trend_") and pm_price <= SHADOW_PM_MAX:
                decision = "allow"
                reason = "late lock aligned with ongoing 5m regime"
            elif score >= SHADOW_LIVE_WATCH_MIN_SCORE and delta_pct >= SHADOW_EARLY_DELTA_MIN * 0.8:
                decision = "watch"
                reason = "promising shadow pattern but not strong enough for allow"
            else:
                decision = "neutral"
                reason = "candidate did not clear live-shadow quality bar"

        return {
            "decision": decision,
            "reason": reason,
            "profile": profile,
            "score": round(score, 4),
            "candidate": candidate,
            "market_regime": regime,
            "stable_ticks": stable_ticks,
            "recent_5m_streak": recent_streak,
            "window_progress_pct": round(progress, 4),
            "underpricing_score": round(underpricing_score, 4),
            "pm_vs_delta_gap": round(pm_gap, 4),
        }

    def _finalize_window_summaries(self, close_ts: int):
        to_remove = []
        for slug, history in self.window_history.items():
            try:
                window_close_ts = int(str(slug).rsplit("-", 1)[-1]) + 300
            except Exception:
                window_close_ts = close_ts
            if window_close_ts > close_ts:
                continue
            points = list(history)
            if points:
                last_delta = float(points[-1].get("delta_pct", 0) or 0)
                direction = points[-1].get("direction") if points[-1].get("direction") in ("Up", "Down") else ("Up" if last_delta >= 0 else "Down")
                self.closed_window_summaries.append({
                    "slug": slug,
                    "direction": direction,
                    "delta_pct": last_delta,
                    "pm_price": float(points[-1].get("pm_price", 0) or 0),
                })
            to_remove.append(slug)
        for slug in to_remove:
            self.window_history.pop(slug, None)
            self.shadow_window_state.pop(slug, None)

    def _enter(
        self,
        market: dict,
        ta: dict,
        seconds_left: float,
        trade_amount: float,
        signal_tier: str,
        signal_tier_reason: str,
    ) -> dict:
        price        = market["winner_price"]
        expected_pnl = (trade_amount / price) - trade_amount
        expected_pct = expected_pnl / trade_amount * 100
        crypto       = market["crypto"]
        market_prob  = float(price or 0)
        model_prob   = estimate_model_prob(
            ta.get("direction"),
            market["winner_side"],
            ta.get("confidence", 0),
            market_prob,
            ta.get("score", 0),
        )
        edge         = model_prob - market_prob

        log(f"🟢 ENTERING [{crypto} {market['winner_side']}] invested=${trade_amount:.2f} expected_pnl=+${expected_pnl:.2f} (+{expected_pct:.1f}%)")
        log(f"   {market['title'][:60]} | price={price:.3f} | time_left={seconds_left:.1f}s")
        log(f"   Price:{ta.get('current_price',0):.2f} | "
            f"delta:{ta.get('delta_pct',0):.4f}% | "
            f"conf:{ta.get('confidence',0):.0%} | "
            f"1m={ta.get('indicator_confirm',0):+.2f} | "
            f"model={model_prob:.3f} market={market_prob:.3f} edge={edge:+.3f}")

        if self.paper or self.dry_run:
            mode = "📄 PAPER" if self.paper else "🔍 DRY RUN"
            log(f"   {mode} — not executed on chain")
            execution = {
                "ok": True,
                "failure_type": "",
                "detail": mode,
                "order_status": "paper",
                "order_id": "",
            }
        else:
            execution = execute_buy(
                market["winner_token"], trade_amount, price,
                self.private_key, self.proxy_wallet
            )

        if execution.get("ok"):
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
                "indicator_confirm": ta.get("indicator_confirm", 0),
                "signal_tier":  signal_tier,
                "signal_tier_reason": signal_tier_reason,
                "model_prob":   model_prob,
                "market_prob":  market_prob,
                "edge":         edge,
                "timestamp":    ts_str(),
            })
            log(f"   ✅ Trade #{len(self.trades)} recorded [{crypto}]")

        return execution

    def _check_previous_round(self, close_ts: int):
        """
        Проверяет результаты сигналов из уже закрывшихся раундов на Polymarket.
        Обновляет signals.json с результатами:
          - для entered=True: фактический результат (WIN/LOSS, realized_pnl)
          - для entered=False: контрфактический pnl_if_entered для оффлайн-анализа
        """
        try:
            result_cache = {}

            def resolve_records(records: list, persist_fn, apply_bank_updates: bool) -> None:
                updated = False
                for i, sig in enumerate(records):
                    entered = bool(sig.get("entered"))
                    if entered and "realized_pnl" in sig:
                        continue
                    if (not entered) and ("winner" in sig and "pnl_if_entered" in sig):
                        continue

                    sig_close_ts = sig.get("market_close_ts")
                    if sig_close_ts is not None:
                        if sig_close_ts > close_ts:
                            continue
                    else:
                        sig_ts_str = sig.get("timestamp", "")
                        if not sig_ts_str:
                            continue
                        try:
                            sig_dt = datetime.strptime(sig_ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                            sig_unix = int(sig_dt.timestamp())
                        except Exception:
                            continue
                        if sig_unix > close_ts:
                            continue

                    crypto = sig.get("coin", "")
                    side = sig.get("side", "")
                    if not crypto or not side:
                        continue

                    slug = sig.get("market_slug")
                    if not slug:
                        prefix = "btc-updown-5m" if crypto == "BTC" else "eth-updown-5m"
                        start_ts = close_ts - 300
                        slug = f"{prefix}-{start_ts}"

                    if slug not in result_cache:
                        try:
                            r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=5)
                            r.raise_for_status()
                            data = r.json()
                            if not data:
                                result_cache[slug] = None
                                continue
                            event = data[0]
                            if event.get("active") and not event.get("closed"):
                                result_cache[slug] = None
                                continue
                            markets = event.get("markets", [])
                            if not markets:
                                result_cache[slug] = None
                                continue
                            market = markets[0]
                            outcome_prices = json.loads(market.get("outcomePrices", "[]"))
                            outcomes = json.loads(market.get("outcomes", "[]"))
                            if len(outcome_prices) < 2 or len(outcomes) < 2:
                                result_cache[slug] = None
                                continue
                            prices = [float(p) for p in outcome_prices]
                            winner_idx = 0 if prices[0] >= prices[1] else 1
                            result_cache[slug] = (outcomes[winner_idx], outcomes[1 - winner_idx])
                        except Exception as e:
                            log(f"   ⚠️  Result check failed for {slug}: {e}")
                            result_cache[slug] = None
                            continue

                    outcome = result_cache.get(slug)
                    if outcome is None:
                        continue
                    winner, loser = outcome

                    won = (side == winner)
                    entry_price = sig.get("pm", 0)
                    trade_amount = sig.get("amount", self.amount)

                    records[i]["won"] = won
                    records[i]["winner"] = winner
                    records[i]["loser"] = loser
                    records[i]["resolved_at"] = ts_str()

                    if won:
                        payout = trade_amount / entry_price if entry_price > 0 else trade_amount
                        pnl_if_entered = payout - trade_amount
                        result = "WIN"
                    else:
                        payout = 0
                        pnl_if_entered = -trade_amount
                        result = "LOSS"

                    records[i]["pnl_if_entered"] = round(pnl_if_entered, 2)

                    if entered:
                        records[i]["result"] = result
                        records[i]["realized_pnl"] = round(pnl_if_entered, 2)
                        records[i]["payout"] = round(payout, 2)
                        if apply_bank_updates:
                            self.bank_balance += pnl_if_entered
                            log(f"   🏁 [{crypto}] {result} | side={side} winner={winner} | "
                                f"pnl=${pnl_if_entered:+.2f} | bank=${self.bank_balance:.2f}")
                    else:
                        records[i]["counterfactual_result"] = result

                    updated = True

                if updated:
                    persist_fn(records)

            if SIGNALS_FILE.exists():
                signals = load_signals_file()
                if isinstance(signals, list):
                    resolve_records(signals, save_signals_file, apply_bank_updates=True)

            if WINDOW_SAMPLES_FILE.exists():
                samples = load_window_samples_file()
                if isinstance(samples, list):
                    resolve_records(samples, save_window_samples_file, apply_bank_updates=False)

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
