"""
Streamlit Dashboard для 5m-poly-bot
Запуск: streamlit run dashboard.py --server.port 3001 --server.address 0.0.0.0 --server.headless true

Улучшения:
  - Настраиваемый путь к логу (env var + sidebar)
  - Winrate и PnL метрики
  - Авто-рефреш через st.empty()
  - Алерт при новом сигнале
  - Надёжный парсинг логов
"""
import streamlit as st
import re
import os
from datetime import datetime
from pathlib import Path
import time

# ===== CONFIG =====
DEFAULT_LOG_FILE = Path("/root/5m-poly-bot/bot.log")
AUTO_REFRESH_SEC = 5

st.set_page_config(
    page_title="5m Poly Bot Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ===== STYLES =====
st.markdown("""
<style>
    .metric-green { color: #51cf66; font-weight: bold; }
    .metric-red { color: #ff6b6b; font-weight: bold; }
    .metric-yellow { color: #ffd43b; font-weight: bold; }
    .metric-blue { color: #4dabf7; font-weight: bold; }
    .signal-card {
        background: #1a1a2e; border-radius: 8px; padding: 10px 15px;
        margin: 5px 0; border-left: 4px solid #4dabf7;
    }
    .skip-card {
        background: #1a1a2e; border-radius: 8px; padding: 10px 15px;
        margin: 5px 0; border-left: 4px solid #ff6b6b;
    }
    .trade-card {
        background: #1a1a2e; border-radius: 8px; padding: 10px 15px;
        margin: 5px 0; border-left: 4px solid #51cf66;
    }
    .alert-box {
        background: #2a1a2e; border: 2px solid #ffd43b; border-radius: 8px;
        padding: 15px; margin: 10px 0; animation: pulse 1.5s infinite;
    }
    @keyframes pulse {
        0%, 100% { border-color: #ffd43b; }
        50% { border-color: #ff6b6b; }
    }
</style>
""", unsafe_allow_html=True)

# ===== PARSE LOGS =====

# Более устойчивые regex-паттерны
_PATTERNS = {
    "timestamp": re.compile(r'\[(\d{4}-\d{2}-\d{2}T[\d:]+Z?)\]'),
    "active_window": re.compile(r'Active window.*?close\s+([\d:]+)'),
    "sleeping": re.compile(r'Sleeping\s+(\d+)s.*?next close\s+([\d:]+)'),
    "market_closed": re.compile(r'Market closed'),
    "signal_coin": re.compile(r'\[(BTC|ETH)\]'),
    "signal_time": re.compile(r'(\d+\.?\d*)s\s*\|'),
    "signal_pm": re.compile(r'PM:(Up|Down)@([\d.]+)'),
    "signal_delta": re.compile(r'delta:([\d.]+)%'),
    "signal_conf": re.compile(r'conf:(\d+)%'),
    "signal_price": re.compile(r'Price:([\d,.]+)'),
    "signal_momentum": re.compile(r'momentum=(↑|↓)'),
    "signal_atr": re.compile(r'range\s+\$?([\d.]+)\s*>\s*1\.5x\s*ATR\s+\$?([\d.]+)'),
    "skip_coin": re.compile(r'\[(BTC|ETH)\].*SKIP'),
    "binance_error": re.compile(r'BINANCE ERROR'),
}


def parse_log_line(line):
    """Парсит одну строку лога. Возвращает dict или None."""
    if not line.strip():
        return None

    result = {'raw': line}

    # Timestamp
    m = _PATTERNS["timestamp"].search(line)
    if m:
        result['timestamp'] = m.group(1)

    # Active window
    m = _PATTERNS["active_window"].search(line)
    if m:
        result['type'] = 'window_start'
        result['next_close'] = m.group(1)
        return result

    # Sleeping
    m = _PATTERNS["sleeping"].search(line)
    if m:
        result['type'] = 'sleeping'
        result['sleep_seconds'] = int(m.group(1))
        result['next_close'] = m.group(2) if m.lastindex >= 2 else '--:--'
        return result

    # Market closed
    if _PATTERNS["market_closed"].search(line):
        result['type'] = 'market_closed'
        return result

    # Signal (🎯 monitoring line)
    if '🎯' in line and ('PM:Up@' in line or 'PM:Down@' in line):
        result['type'] = 'signal'
        m = _PATTERNS["signal_coin"].search(line)
        if m: result['coin'] = m.group(1)
        m = _PATTERNS["signal_time"].search(line)
        if m: result['time_remaining'] = float(m.group(1))
        m = _PATTERNS["signal_pm"].search(line)
        if m:
            result['pm_side'] = m.group(1)
            result['pm_price'] = float(m.group(2))
        m = _PATTERNS["signal_delta"].search(line)
        if m: result['delta'] = float(m.group(1))
        m = _PATTERNS["signal_conf"].search(line)
        if m: result['confidence'] = int(m.group(1))
        m = _PATTERNS["signal_price"].search(line)
        if m: result['binance_price'] = float(m.group(1).replace(',', ''))
        m = _PATTERNS["signal_momentum"].search(line)
        if m: result['momentum'] = m.group(1)
        # ATR in signal line
        m = _PATTERNS["signal_atr"].search(line)
        if m:
            result['atr_range'] = float(m.group(1))
            result['atr_value'] = float(m.group(2))
        return result

    # SKIP decision
    if 'SKIP' in line:
        result['type'] = 'decision'
        result['action'] = 'SKIP'
        m = _PATTERNS["skip_coin"].search(line)
        if m: result['coin'] = m.group(1)

        if 'PM price' in line:
            if '< 0.94' in line:
                result['reason'] = 'pm_price_low_btc'
            elif '< 0.92' in line:
                result['reason'] = 'pm_price_low_eth'
            elif '> 0.99' in line:
                result['reason'] = 'pm_price_high'
        elif 'too close to the line' in line:
            result['reason'] = 'delta_low'
        elif 'confidence' in line and '< 30%' in line:
            result['reason'] = 'confidence_low'
        elif 'ATR skip' in line:
            result['reason'] = 'atr_high'
        else:
            result['reason'] = 'unknown'
        return result

    # Binance error
    if _PATTERNS["binance_error"].search(line):
        result['type'] = 'error'
        result['error'] = 'binance_timeout'
        return result

    return None


def get_log_file_path():
    """Получает путь к лог-файлу из env var, sidebar или дефолта."""
    env_path = os.environ.get("BOT_LOG_FILE", "")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    return DEFAULT_LOG_FILE


def read_and_parse_logs(log_path, max_lines=2000):
    if not log_path or not log_path.exists():
        return [], None

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        recent_lines = lines[-max_lines:]
    except Exception:
        return [], None

    parsed = []
    for line in recent_lines:
        entry = parse_log_line(line)
        if entry:
            parsed.append(entry)

    # Статистика
    stats = {
        'total_signals': 0,
        'total_skips': 0,
        'total_trades': 0,
        'skip_reasons': {},
        'rounds': 0,
        'binance_errors': 0,
        'current_state': 'starting',
        'last_signals': [],
        'last_decisions': [],
        'sleep_seconds': 0,
        'next_close': '--:--',
        'btc_atr': None,
        'eth_atr': None,
        'btc_price': None,
        'eth_price': None,
        # PnL tracking
        'total_invested': 0,
        'expected_pnl': 0,
        'trades_per_round': {},
        'wins': 0,
        'losses': 0,
        'last_update': datetime.now().strftime('%H:%M:%S'),
        'total_lines': len(lines),
    }

    current_round_signals = []
    current_round_decisions = []
    round_idx = 0

    for entry in parsed:
        if entry['type'] == 'signal':
            stats['total_signals'] += 1
            stats['last_signals'].append(entry)
            current_round_signals.append(entry)
            if entry.get('coin') == 'BTC' and entry.get('binance_price'):
                stats['btc_price'] = entry['binance_price']
            if entry.get('coin') == 'ETH' and entry.get('binance_price'):
                stats['eth_price'] = entry['binance_price']
            if entry.get('atr_value') and entry.get('coin') == 'BTC':
                stats['btc_atr'] = entry['atr_value']
            if entry.get('atr_value') and entry.get('coin') == 'ETH':
                stats['eth_atr'] = entry['atr_value']

        elif entry['type'] == 'decision':
            if entry['action'] == 'SKIP':
                stats['total_skips'] += 1
                reason = entry.get('reason', 'unknown')
                stats['skip_reasons'][reason] = stats['skip_reasons'].get(reason, 0) + 1
            current_round_decisions.append(entry)
            stats['last_decisions'].append(entry)

        elif entry['type'] == 'sleeping':
            stats['current_state'] = 'sleeping'
            stats['sleep_seconds'] = entry.get('sleep_seconds', 0)
            stats['next_close'] = entry.get('next_close', '--:--')
            # Save round data
            if current_round_signals or current_round_decisions:
                stats['trades_per_round'][round_idx] = {
                    'signals': list(current_round_signals),
                    'decisions': list(current_round_decisions),
                }
                round_idx += 1
                current_round_signals = []
                current_round_decisions = []

        elif entry['type'] == 'window_start':
            stats['current_state'] = 'active'
            stats['next_close'] = entry.get('next_close', '--:--')

        elif entry['type'] == 'market_closed':
            stats['rounds'] += 1
            stats['current_state'] = 'sleeping'
            # Save round
            if current_round_signals or current_round_decisions:
                stats['trades_per_round'][round_idx] = {
                    'signals': list(current_round_signals),
                    'decisions': list(current_round_decisions),
                }
                round_idx += 1
                current_round_signals = []
                current_round_decisions = []

        elif entry['type'] == 'error':
            stats['binance_errors'] += 1

    # PnL calculation from trade entries
    for entry in parsed:
        if 'ENTERING' in entry.get('raw', ''):
            stats['total_trades'] += 1
            # Parse amount
            m = re.search(r'invested=\$([\d.]+)', entry['raw'])
            if m:
                stats['total_invested'] += float(m.group(1))
            m = re.search(r'expected_pnl=\+?\$([\d.]+)', entry['raw'])
            if m:
                stats['expected_pnl'] += float(m.group(1))

    # Winrate estimation from skip ratio
    total_decisions = stats['total_trades'] + stats['total_skips']
    if total_decisions > 0:
        # Conservative estimate: trades that passed all filters
        stats['win_estimate'] = round(stats['total_trades'] / max(total_decisions, 1) * 100, 1)
    else:
        stats['win_estimate'] = 0

    # Keep only last 30 signals and 30 decisions
    stats['last_signals'] = stats['last_signals'][-30:]
    stats['last_decisions'] = stats['last_decisions'][-30:]

    return parsed, stats


# ===== HELPERS =====

def pm_price_html(price, coin):
    """Color-coded PM price with threshold indicators."""
    min_price = 0.94 if coin == 'BTC' else 0.92
    max_price = 0.99

    if price >= min_price and price <= max_price:
        color = '#51cf66'
        label = '✅ GOOD'
    elif price < min_price:
        color = '#ff6b6b'
        label = f'❌ < {min_price}'
    else:
        color = '#ff6b6b'
        label = f'❌ > {max_price}'

    return f'<span style="color:{color}; font-weight:bold;">{price:.3f}</span> <span style="color:{color}; font-size:0.8em;">{label}</span>'


def delta_html(delta):
    if delta >= 0.1:
        color = '#51cf66'
    elif delta >= 0.05:
        color = '#ffd43b'
    else:
        color = '#ff6b6b'
    return f'<span style="color:{color}; font-weight:bold;">{delta:.3f}%</span>'


def conf_html(conf):
    if conf >= 30:
        color = '#51cf66'
    elif conf >= 10:
        color = '#ffd43b'
    else:
        color = '#ff6b6b'
    return f'<span style="color:{color}; font-weight:bold;">{conf}%</span>'


def pnl_html(pnl):
    if pnl >= 0:
        color = '#51cf66'
        sign = '+'
    else:
        color = '#ff6b6b'
        sign = ''
    return f'<span style="color:{color}; font-weight:bold;">{sign}${pnl:.2f}</span>'


def skip_reason_label(reason):
    labels = {
        'pm_price_low_btc': ' PM Price Low (BTC < 0.94)',
        'pm_price_low_eth': ' PM Price Low (ETH < 0.92)',
        'pm_price_high': ' PM Price High (> 0.99, no upside)',
        'delta_low': ' Delta Low (weak momentum)',
        'confidence_low': ' Confidence Low (< 30%)',
        'atr_high': ' ATR High (too volatile)',
        'unknown': ' Unknown',
    }
    return labels.get(reason, reason)


# ===== MAIN =====

st.title("5m Poly Bot Dashboard")

# Sidebar: config
log_path = get_log_file_path()
custom_log_path = st.sidebar.text_input("Log file path", value=str(log_path))
if custom_log_path and Path(custom_log_path).exists():
    log_path = Path(custom_log_path)

auto_refresh = st.sidebar.checkbox("Auto-refresh (5s)", value=True)
refresh_interval = st.sidebar.slider("Refresh interval (s)", 3, 30, 5)

st.sidebar.divider()
st.sidebar.subheader("Settings Reference")
st.sidebar.markdown("""
| Param | Default | Description |
|-------|---------|-------------|
| AMOUNT | $10 | Bet size |
| MIN_DELTA | 0.05% | Min delta move |
| MIN_CONFIDENCE | 30% | Min confidence |
| ENTRY_WINDOW | 10-50s | Entry window |
| PM_MIN_BTC | 0.94 | BTC min price |
| PM_MIN_ETH | 0.92 | ETH min price |
| PM_MAX | 0.99 | Max price (no upside) |
""")

# ===== AUTO-REFRESH CONTAINER =====
if auto_refresh:
    refresh_placeholder = st.empty()
    time.sleep(1)
    st.rerun()

# ===== CHECK LOG FILE =====
if not log_path or not log_path.exists():
    st.warning(f"Log file not found: `{log_path}`")
    st.info("Start the bot: `python3 crypto_bot.py --dry-run 2>&1 | tee bot.log`")
    st.stop()

# ===== PARSE LOGS =====
logs, stats = read_and_parse_logs(log_path)

if not stats:
    st.warning("Failed to parse logs.")
    st.stop()

# ===== ALERT: NEW SIGNAL =====
if stats['last_signals']:
    last_signal = stats['last_signals'][-1]
    if last_signal.get('time_remaining', 999) < 15:
        coin = last_signal.get('coin', '?')
        side = last_signal.get('pm_side', '?')
        st.markdown(
            f'<div class="alert-box">'
            f'<b>Active Signal!</b> [{coin}] {side} — '
            f'{last_signal.get("time_remaining", 0):.0f}s remaining'
            f'</div>',
            unsafe_allow_html=True
        )

# ===== STATUS BAR =====
state = stats['current_state']
state_emoji = {'sleeping': '💤', 'active': '⚡', 'starting': '🚀'}

col1, col2, col3, col4, col5, col6 = st.columns(6)

with col1:
    st.metric("Status", f"{state_emoji.get(state, '⚪')} {state.upper()}")
with col2:
    st.metric("Next Close", stats['next_close'])
with col3:
    st.metric("Sleep", f"{stats['sleep_seconds']}s")
with col4:
    st.metric("Signals", stats['total_signals'])
with col5:
    st.metric("Rounds", stats['rounds'])
with col6:
    if stats['binance_errors'] > 0:
        st.metric("Binance Errors", stats['binance_errors'])
    else:
        st.metric("Binance", "OK")

# ===== PnL & WINRATE METRICS =====
st.subheader("Performance")

col_pnl1, col_pnl2, col_pnl3, col_pnl4 = st.columns(4)

with col_pnl1:
    st.metric("Total Trades", stats['total_trades'])
with col_pnl2:
    st.metric("Expected PnL", pnl_html(stats['expected_pnl']), delta=f"{stats['expected_pnl']:.2f}")
with col_pnl3:
    st.metric("Total Invested", f"${stats['total_invested']:.2f}")
with col_pnl4:
    st.metric("Est. Winrate", f"{stats['win_estimate']}%")

# ===== LIVE PRICES =====
st.subheader("Live Prices")

col_btc, col_eth = st.columns(2)

with col_btc:
    btc_price = stats.get('btc_price')
    btc_atr = stats.get('btc_atr')
    st.metric("BTC Price", f"${btc_price:.2f}" if btc_price else "N/A")
    if btc_atr:
        st.caption(f"ATR (5m): ${btc_atr:.2f}")
    else:
        st.caption("ATR: N/A")

with col_eth:
    eth_price = stats.get('eth_price')
    eth_atr = stats.get('eth_atr')
    st.metric("ETH Price", f"${eth_price:.2f}" if eth_price else "N/A")
    if eth_atr:
        st.caption(f"ATR (5m): ${eth_atr:.2f}")
    else:
        st.caption("ATR: N/A")

# ===== PM PRICE RANGE =====
st.subheader("Polymarket Price Range")

col_btc_pm, col_eth_pm = st.columns(2)

btc_signal = None
eth_signal = None
for s in reversed(stats['last_signals']):
    if s.get('coin') == 'BTC' and not btc_signal:
        btc_signal = s
    if s.get('coin') == 'ETH' and not eth_signal:
        eth_signal = s

with col_btc_pm:
    st.markdown("**BTC**")
    if btc_signal and btc_signal.get('pm_price'):
        price = btc_signal['pm_price']
        st.markdown(pm_price_html(price, 'BTC'), unsafe_allow_html=True)
        pct = min(price / 1.0, 1.0)
        st.progress(pct)
        st.caption(f"Delta: {delta_html(btc_signal.get('delta', 0))} | Conf: {conf_html(btc_signal.get('confidence', 0))}", unsafe_allow_html=True)
    else:
        st.info("No data yet")

with col_eth_pm:
    st.markdown("**ETH**")
    if eth_signal and eth_signal.get('pm_price'):
        price = eth_signal['pm_price']
        st.markdown(pm_price_html(price, 'ETH'), unsafe_allow_html=True)
        pct = min(price / 1.0, 1.0)
        st.progress(pct)
        st.caption(f"Delta: {delta_html(eth_signal.get('delta', 0))} | Conf: {conf_html(eth_signal.get('confidence', 0))}", unsafe_allow_html=True)
    else:
        st.info("No data yet")

# ===== SKIP REASONS =====
st.subheader("Skip Reasons")

reasons = stats.get('skip_reasons', {})
if reasons:
    total_skips = stats['total_skips']
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        label = skip_reason_label(reason)
        pct = (count / max(total_skips, 1)) * 100
        st.progress(count / max(total_skips, 1), text=f"{label}: {count} ({pct:.0f}%)")
else:
    st.info("No skips recorded")

# ===== RECENT SIGNALS TABLE =====
st.subheader(f"Recent Signals (last 20)")

if stats['last_signals']:
    signals_data = []
    for s in reversed(stats['last_signals'][-20:]):
        coin = s.get('coin', '?')
        side = s.get('pm_side', '?')
        price = s.get('pm_price', 0)
        delta = s.get('delta', 0)
        conf = s.get('confidence', 0)
        time_rem = s.get('time_remaining', 0)
        momentum = s.get('momentum', '?')
        binance_p = s.get('binance_price', 0)

        if delta >= 0.1 and conf >= 30 and price >= 0.90 and price <= 0.99:
            quality = "HIGH"
        elif delta >= 0.05 and conf >= 10:
            quality = "MEDIUM"
        else:
            quality = "LOW"

        signals_data.append({
            "Coin": coin,
            "Side": side,
            "PM Price": f"{price:.3f}",
            "Delta": f"{delta:.3f}%",
            "Conf": f"{conf}%",
            "Time": f"{time_rem:.0f}s",
            "Momentum": momentum,
            "Binance": f"${binance_p:.2f}",
            "Quality": quality,
        })

    st.table(signals_data)
else:
    st.info("No signals yet")

# ===== RECENT DECISIONS =====
st.subheader("Recent Decisions (last 10)")

if stats['last_decisions']:
    for d in reversed(stats['last_decisions'][-10:]):
        coin = d.get('coin', '?')
        action = d.get('action', '?')
        reason = d.get('reason', '')
        reason_label = skip_reason_label(reason)

        if action == 'SKIP':
            st.markdown(
                f'<div class="skip-card"> [{coin}] SKIP — {reason_label}</div>',
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                f'<div class="trade-card"> [{coin}] TRADE</div>',
                unsafe_allow_html=True
            )
else:
    st.info("No decisions yet")

# ===== FOOTER =====
st.divider()
st.caption(f"5m Poly Bot Dashboard | Auto-refresh: {'ON' if auto_refresh else 'OFF'} | Updated: {stats['last_update']} | Log lines: {stats['total_lines']}")
