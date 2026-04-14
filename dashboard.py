"""
Streamlit Dashboard для 5m-poly-bot
Запуск: streamlit run dashboard.py --server.port 3001 --server.address 0.0.0.0 --server.headless true
"""
import streamlit as st
import re
import os
from datetime import datetime
from pathlib import Path
import time

# ===== CONFIG =====
LOG_FILE = Path("/root/5m-poly-bot/bot.log")
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
    .price-high { color: #ff6b6b; }
    .price-low { color: #ff6b6b; }
    .price-good { color: #51cf66; }
    .delta-high { color: #51cf66; }
    .delta-med { color: #ffd43b; }
    .delta-low { color: #ff6b6b; }
</style>
""", unsafe_allow_html=True)

# ===== PARSE LOGS =====
def parse_log_line(line):
    result = {'raw': line}
    
    ts_match = re.search(r'\[(\d{4}-\d{2}-\d{2}T[\d:]+Z?)\]', line)
    if ts_match:
        result['timestamp'] = ts_match.group(1)
    
    if 'Active window' in line:
        result['type'] = 'window_start'
        m = re.search(r'close ([\d:]+)', line)
        if m: result['next_close'] = m.group(1)
    
    if 'Sleeping' in line:
        result['type'] = 'sleeping'
        m = re.search(r'Sleeping (\d+)s', line)
        if m: result['sleep_seconds'] = int(m.group(1))
        m = re.search(r'next close ([\d:]+)', line)
        if m: result['next_close'] = m.group(1)
    
    if 'Market closed' in line:
        result['type'] = 'market_closed'
    
    if '🎯' in line or 'PM:Up@' in line or 'PM:Down@' in line:
        result['type'] = 'signal'
        m = re.search(r'\[(BTC|ETH)\]', line)
        if m: result['coin'] = m.group(1)
        m = re.search(r'(\d+\.?\d*)s \|', line)
        if m: result['time_remaining'] = float(m.group(1))
        m = re.search(r'PM:(Up|Down)@([\d.]+)', line)
        if m:
            result['pm_side'] = m.group(1)
            result['pm_price'] = float(m.group(2))
        m = re.search(r'delta:([\d.]+)%', line)
        if m: result['delta'] = float(m.group(1))
        m = re.search(r'conf:(\d+)%', line)
        if m: result['confidence'] = int(m.group(1))
        m = re.search(r'Price:([\d,.]+)', line)
        if m: result['binance_price'] = float(m.group(1).replace(',', ''))
        m = re.search(r'momentum=(↑|↓)', line)
        if m: result['momentum'] = m.group(1)
        if 'ATR skip' in line:
            m_atr = re.search(r'range \$(\d+\.?\d*) > 1\.5x ATR \$(\d+\.?\d*)', line)
            if m_atr:
                result['atr_range'] = float(m_atr.group(1))
                result['atr_value'] = float(m_atr.group(2))
    
    if 'SKIP' in line:
        result['type'] = 'decision'
        result['action'] = 'SKIP'
        m = re.search(r'\[(BTC|ETH)\]', line)
        if m: result['coin'] = m.group(1)
        
        if 'PM price' in line and '< 0.94' in line:
            result['reason'] = 'pm_price_low_btc'
        elif 'PM price' in line and '< 0.92' in line:
            result['reason'] = 'pm_price_low_eth'
        elif 'PM price' in line and '> 0.99' in line:
            result['reason'] = 'pm_price_high'
        elif 'too close to the line' in line:
            result['reason'] = 'delta_low'
        elif 'confidence' in line and '< 30%' in line:
            result['reason'] = 'confidence_low'
        elif 'ATR skip' in line:
            result['reason'] = 'atr_high'
        else:
            result['reason'] = 'unknown'
    
    if 'BINANCE ERROR' in line:
        result['type'] = 'error'
        result['error'] = 'binance_timeout'
    
    return result if result.get('type') else None


def read_and_parse_logs(max_lines=1000):
    if not LOG_FILE.exists():
        return [], None
    
    try:
        lines = LOG_FILE.read_text().splitlines()
        recent_lines = lines[-max_lines:]
    except Exception:
        return [], None
    
    parsed = []
    for line in recent_lines:
        if not line.strip():
            continue
        entry = parse_log_line(line)
        if entry:
            parsed.append(entry)
    
    # Stats
    stats = {
        'total_signals': 0,
        'total_skips': 0,
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
    }
    
    for entry in parsed:
        if entry['type'] == 'signal':
            stats['total_signals'] += 1
            stats['last_signals'].append(entry)
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
            stats['last_decisions'].append(entry)
        elif entry['type'] == 'sleeping':
            stats['current_state'] = 'sleeping'
            stats['sleep_seconds'] = entry.get('sleep_seconds', 0)
            stats['next_close'] = entry.get('next_close', '--:--')
        elif entry['type'] == 'window_start':
            stats['current_state'] = 'active'
            stats['next_close'] = entry.get('next_close', '--:--')
        elif entry['type'] == 'market_closed':
            stats['rounds'] += 1
            stats['current_state'] = 'sleeping'
        elif entry['type'] == 'error':
            stats['binance_errors'] += 1
    
    # Keep only last 30 signals
    stats['last_signals'] = stats['last_signals'][-30:]
    stats['last_decisions'] = stats['last_decisions'][-30:]
    
    return parsed, stats


# ===== HELPERS =====
def pm_price_html(price, coin):
    """Color-coded PM price with threshold indicators."""
    min_price = 0.94 if coin == 'BTC' else 0.92
    max_price = 0.99
    
    if price >= min_price and price <= max_price:
        color = '#51cf66'  # green
        label = '✅ GOOD'
    elif price < min_price:
        color = '#ff6b6b'  # red
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


def skip_reason_label(reason):
    labels = {
        'pm_price_low_btc': ' PM Price Low (BTC < 0.94)',
        'pm_price_low_eth': ' PM Price Low (ETH < 0.92)',
        'pm_price_high': '📈 PM Price High (> 0.99, no upside)',
        'delta_low': '⚖️ Delta Low (weak momentum)',
        'confidence_low': '🎯 Confidence Low (< 30%)',
        'atr_high': '📊 ATR High (too volatile)',
        'unknown': '❓ Unknown',
    }
    return labels.get(reason, reason)


# ===== MAIN UI =====
st.title("📈 5m Poly Bot Dashboard")

# Auto-refresh
auto_refresh = st.sidebar.checkbox("🔄 Auto-refresh (5s)", value=True)

if auto_refresh:
    time.sleep(1)  # Brief pause so UI is readable
    st.rerun()

# Parse
logs, stats = read_and_parse_logs()

if not stats:
    st.warning("⏳ Лог-файл не найден. Запустите бота: `python3 crypto_bot.py --dry-run 2>&1 | tee bot.log`")
    st.stop()

# ===== HEADER: STATUS BAR =====
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
        st.metric("Binance", "✅ OK")

# ===== LIVE PRICES =====
st.subheader("💰 Live Prices")

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

# ===== PM PRICE RANGE VISUALIZATION =====
st.subheader("📊 Polymarket Price Range")

col_btc_pm, col_eth_pm = st.columns(2)

# Get latest signal per coin
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
        # Progress bar
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
st.subheader("🚫 Skip Reasons")

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
st.subheader("📡 Recent Signals (last 20)")

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
        
        # Quality
        if delta >= 0.1 and conf >= 30 and price >= 0.90 and price <= 0.99:
            quality = "🟢 HIGH"
        elif delta >= 0.05 and conf >= 10:
            quality = "🟡 MEDIUM"
        else:
            quality = "🔴 LOW"
        
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
st.subheader("🎯 Recent Decisions (last 10)")

if stats['last_decisions']:
    for d in reversed(stats['last_decisions'][-10:]):
        coin = d.get('coin', '?')
        action = d.get('action', '?')
        reason = d.get('reason', '')
        reason_label = skip_reason_label(reason)
        
        if action == 'SKIP':
            st.markdown(
                f'<div class="skip-card">🔴 <b>[{coin}]</b> SKIP — {reason_label}</div>',
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                f'<div class="trade-card">🟢 <b>[{coin}]</b> TRADE</div>',
                unsafe_allow_html=True
            )
else:
    st.info("No decisions yet")

# ===== SETTINGS REFERENCE =====
with st.sidebar:
    st.subheader("⚙️ Settings Reference")
    st.markdown("""
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
    
    st.divider()
    st.caption(f"Log lines: {len(logs)} | Updated: {datetime.now().strftime('%H:%M:%S')}")

# ===== FOOTER =====
st.divider()
st.caption(f"5m Poly Bot Dashboard | Auto-refresh: {'ON' if auto_refresh else 'OFF'}")
