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
DEFAULT_LOG_FILE = Path("/root/5m-poly-bot/bot.log")

st.set_page_config(
    page_title="5m Poly Bot Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ===== CSS =====
st.markdown("""
<style>
    .stMetric { background: #1a1a2e; border-radius: 8px; padding: 12px; }
    .alert-box {
        background: #2a2a1e; border: 2px solid #ffd43b; border-radius: 8px;
        padding: 12px 20px; margin: 10px 0;
    }
    .signal-card {
        background: #1a1a2e; border-radius: 6px; padding: 8px 14px;
        margin: 4px 0; border-left: 3px solid #4dabf7; font-size: 0.9em;
    }
    .skip-card {
        background: #1a1a2e; border-radius: 6px; padding: 8px 14px;
        margin: 4px 0; border-left: 3px solid #ff6b6b; font-size: 0.9em;
    }
    .trade-card {
        background: #1a1a2e; border-radius: 6px; padding: 8px 14px;
        margin: 4px 0; border-left: 3px solid #51cf66; font-size: 0.9em;
    }
    .empty-state {
        text-align: center; padding: 40px; color: #888;
    }
</style>
""", unsafe_allow_html=True)


# ===== REGEX PATTERNS =====
_PAT = {
    "ts":          re.compile(r'\[(\d{4}-\d{2}-\d{2}T[\d:]+Z?)\]'),
    "active_win":  re.compile(r'Active window.*?close\s+([\d:]+)'),
    "sleeping":    re.compile(r'Sleeping\s+(\d+)s'),
    "sleep_close": re.compile(r'next close\s+([\d:]+)'),
    "closed":      re.compile(r'Market closed'),
    "coin":        re.compile(r'\[(BTC|ETH)\]'),
    "time_left":   re.compile(r'(\d+\.?\d*)s\s*\|'),
    "pm_side":     re.compile(r'PM:(Up|Down)@([\d.]+)'),
    "delta":       re.compile(r'delta:([\d.]+)%'),
    "conf":        re.compile(r'conf:(\d+)%'),
    "price":       re.compile(r'Price:([\d,.]+)'),
    "momentum":    re.compile(r'momentum=(↑|↓)'),
    "atr":         re.compile(r'range\s+\$?([\d.]+)\s*>\s*1\.5x\s*ATR\s+\$?([\d.]+)'),
    "skip":        re.compile(r'\[(BTC|ETH)\].*SKIP'),
    "binance_err": re.compile(r'BINANCE ERROR'),
    "entering":    re.compile(r'ENTERING \[(BTC|ETH)\]'),
    "invested":    re.compile(r'invested=\$([\d.]+)'),
    "pnl":         re.compile(r'expected_pnl=\+?\$([\d.]+)'),
}


def parse_line(line):
    """Парсит строку лога → dict или None."""
    if not line.strip():
        return None
    r = {'raw': line}
    m = _PAT["ts"].search(line)
    if m:
        r['ts'] = m.group(1)

    if _PAT["active_win"].search(line):
        r['type'] = 'active'
        m2 = _PAT["active_win"].search(line)
        if m2:
            r['next_close'] = m2.group(1)
        return r

    if _PAT["sleeping"].search(line):
        r['type'] = 'sleeping'
        m2 = _PAT["sleeping"].search(line)
        if m2:
            r['sleep_sec'] = int(m2.group(1))
        m3 = _PAT["sleep_close"].search(line)
        if m3:
            r['next_close'] = m3.group(1)
        return r

    if _PAT["closed"].search(line):
        r['type'] = 'closed'
        return r

    if '🎯' in line and ('PM:Up@' in line or 'PM:Down@' in line):
        r['type'] = 'signal'
        m2 = _PAT["coin"].search(line)
        if m2: r['coin'] = m2.group(1)
        m2 = _PAT["time_left"].search(line)
        if m2: r['time'] = float(m2.group(1))
        m2 = _PAT["pm_side"].search(line)
        if m2:
            r['pm_side'] = m2.group(1)
            r['pm_price'] = float(m2.group(2))
        m2 = _PAT["delta"].search(line)
        if m2: r['delta'] = float(m2.group(1))
        m2 = _PAT["conf"].search(line)
        if m2: r['conf'] = int(m2.group(1))
        m2 = _PAT["price"].search(line)
        if m2: r['binance_price'] = float(m2.group(1).replace(',', ''))
        m2 = _PAT["momentum"].search(line)
        if m2: r['momentum'] = m2.group(1)
        m2 = _PAT["atr"].search(line)
        if m2:
            r['atr_range'] = float(m2.group(1))
            r['atr_val'] = float(m2.group(2))
        return r

    if 'SKIP' in line:
        r['type'] = 'skip'
        r['action'] = 'SKIP'
        m2 = _PAT["coin"].search(line)
        if m2: r['coin'] = m2.group(1)
        if 'PM price' in line:
            if '< 0.94' in line:
                r['reason'] = 'pm_price_low_btc'
            elif '< 0.92' in line:
                r['reason'] = 'pm_price_low_eth'
            elif '> 0.99' in line:
                r['reason'] = 'pm_price_high'
        elif 'too close' in line:
            r['reason'] = 'delta_low'
        elif 'confidence' in line and '< 30%' in line:
            r['reason'] = 'conf_low'
        elif 'ATR skip' in line:
            r['reason'] = 'atr_high'
        else:
            r['reason'] = 'unknown'
        return r

    if _PAT["binance_err"].search(line):
        r['type'] = 'error'
        return r

    if _PAT["entering"].search(line):
        r['type'] = 'trade'
        m2 = _PAT["entering"].search(line)
        if m2: r['coin'] = m2.group(1)
        m2 = _PAT["invested"].search(line)
        if m2: r['amount'] = float(m2.group(1))
        m2 = _PAT["pnl"].search(line)
        if m2: r['pnl'] = float(m2.group(1))
        return r

    return None


def parse_logs(log_path, max_lines=3000):
    """Читает и парсит лог → (raw_lines, stats_dict) или (None, None)."""
    if not log_path or not log_path.exists():
        return None, None
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None, None

    parsed = []
    for line in lines[-max_lines:]:
        e = parse_line(line)
        if e:
            parsed.append(e)

    stats = {
        'signals': 0,
        'skips': 0,
        'trades': 0,
        'skip_reasons': {},
        'rounds': 0,
        'errors': 0,
        'state': 'starting',
        'last_signals': [],
        'last_skips': [],
        'last_trades': [],
        'sleep_sec': 0,
        'next_close': '--:--',
        'btc_price': None,
        'eth_price': None,
        'btc_atr': None,
        'eth_atr': None,
        'total_invested': 0.0,
        'expected_pnl': 0.0,
        'last_update': datetime.now().strftime('%H:%M:%S'),
        'total_lines': len(lines),
        'has_data': False,
    }

    for e in parsed:
        t = e.get('type')
        if t == 'signal':
            stats['signals'] += 1
            stats['last_signals'].append(e)
            if e.get('coin') == 'BTC' and e.get('binance_price'):
                stats['btc_price'] = e['binance_price']
            if e.get('coin') == 'ETH' and e.get('binance_price'):
                stats['eth_price'] = e['binance_price']
            if e.get('atr_val'):
                if e.get('coin') == 'BTC':
                    stats['btc_atr'] = e['atr_val']
                else:
                    stats['eth_atr'] = e['atr_val']
        elif t == 'skip':
            stats['skips'] += 1
            stats['last_skips'].append(e)
            r = e.get('reason', 'unknown')
            stats['skip_reasons'][r] = stats['skip_reasons'].get(r, 0) + 1
        elif t == 'trade':
            stats['trades'] += 1
            stats['last_trades'].append(e)
            stats['total_invested'] += e.get('amount', 0)
            stats['expected_pnl'] += e.get('pnl', 0)
        elif t == 'active':
            stats['state'] = 'active'
            stats['next_close'] = e.get('next_close', '--:--')
        elif t == 'sleeping':
            stats['state'] = 'sleeping'
            stats['sleep_sec'] = e.get('sleep_sec', 0)
            stats['next_close'] = e.get('next_close', '--:--')
        elif t == 'closed':
            stats['rounds'] += 1
        elif t == 'error':
            stats['errors'] += 1

    stats['has_data'] = len(parsed) > 0
    stats['last_signals'] = stats['last_signals'][-20:]
    stats['last_skips'] = stats['last_skips'][-20:]
    stats['last_trades'] = stats['last_trades'][-20:]
    return lines, stats


# ===== HELPERS =====
SKIP_LABELS = {
    'pm_price_low_btc': 'PM price BTC < 0.94',
    'pm_price_low_eth': 'PM price ETH < 0.92',
    'pm_price_high':    'PM price > 0.99 (no upside)',
    'delta_low':        'Delta too low (< 0.05%)',
    'conf_low':         'Confidence < 30%',
    'atr_high':         'ATR too high (volatile)',
    'unknown':          'Unknown',
}


# ===== SIDEBAR =====
log_path = Path(os.environ.get("BOT_LOG_FILE", str(DEFAULT_LOG_FILE)))
st.sidebar.text_input("Log file", value=str(log_path), disabled=True)
st.sidebar.caption(f"Lines: loading...")
st.sidebar.divider()

st.sidebar.subheader("Bot Settings")
st.sidebar.markdown("""
| Param | Value |
|-------|-------|
| Bet size | $10 |
| Min delta | 0.05% |
| Min confidence | 30% |
| Entry window | 10–50s before close |
| PM min BTC | 0.94 |
| PM min ETH | 0.92 |
| PM max | 0.99 |
""")

# ===== PARSE =====
raw, stats = parse_logs(log_path)

if not stats:
    st.error(f"Cannot read log file: `{log_path}`")
    st.info("Make sure the bot is running: `python3 crypto_bot.py --dry-run 2>&1 | tee bot.log`")
    st.stop()

# Update sidebar line count
st.sidebar.caption(f"Lines: {stats['total_lines']} | Updated: {stats['last_update']}")

# ===== AUTO-REFRESH =====
auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)

# ===== TITLE + STATUS =====
st.title("5m Poly Bot")

# Status bar — always visible
st_icon = {'sleeping': '💤', 'active': '⚡', 'starting': '🚀'}.get(stats['state'], '⚪')
s1, s2, s3, s4, s5, s6 = st.columns(6)
s1.metric("Bot", f"{st_icon} {stats['state'].upper()}")
s2.metric("Next Close", stats['next_close'])
s3.metric("Sleep", f"{stats['sleep_sec']}s")
s4.metric("Signals", stats['signals'])
s5.metric("Rounds", stats['rounds'])
s6.metric("Binance", f"❌ {stats['errors']}" if stats['errors'] else "OK")

# ===== EMPTY STATE =====
if not stats['has_data']:
    st.divider()
    st.markdown('<div class="empty-state"><h3>⏳ Waiting for data...</h3><p>The bot is running but no signals yet.<br>Check back in a few minutes or verify the log file path.</p></div>', unsafe_allow_html=True)
    st.stop()

# ===== ALERT =====
if stats['last_signals']:
    ls = stats['last_signals'][-1]
    t = ls.get('time', 999)
    if t < 15:
        st.markdown(
            f'<div class="alert-box">⚡ <b>Active Signal!</b> '
            f'[{ls.get("coin","?")}] {ls.get("pm_side","?")} — {t:.0f}s remaining</div>',
            unsafe_allow_html=True
        )

# ===== PERFORMANCE =====
st.divider()
st.subheader("Performance")
p1, p2, p3 = st.columns(3)
p1.metric("Trades (dry run)", stats['trades'])
p2.metric("Expected PnL", f"${stats['expected_pnl']:+.2f}")
p3.metric("Total Invested", f"${stats['total_invested']:.2f}")

# ===== LIVE PRICES =====
st.subheader("Live Prices")
lp1, lp2 = st.columns(2)
with lp1:
    st.metric("BTC", f"${stats['btc_price']:.2f}" if stats['btc_price'] else "N/A")
    if stats['btc_atr']:
        st.caption(f"ATR: ${stats['btc_atr']:.2f}")
with lp2:
    st.metric("ETH", f"${stats['eth_price']:.2f}" if stats['eth_price'] else "N/A")
    if stats['eth_atr']:
        st.caption(f"ATR: ${stats['eth_atr']:.2f}")

# ===== PM PRICE RANGE =====
st.subheader("Polymarket Prices")
pp1, pp2 = st.columns(2)

# Latest signal per coin
btc_s = next((s for s in reversed(stats['last_signals']) if s.get('coin') == 'BTC'), None)
eth_s = next((s for s in reversed(stats['last_signals']) if s.get('coin') == 'ETH'), None)

for col, sig, coin, mn in [(pp1, btc_s, "BTC", 0.94), (pp2, eth_s, "ETH", 0.92)]:
    with col:
        st.markdown(f"**{coin}**")
        if sig and sig.get('pm_price'):
            p = sig['pm_price']
            if mn <= p <= 0.99:
                color, label = "#51cf66", "GOOD"
            elif p < mn:
                color, label = "#ff6b6b", f"LOW (< {mn})"
            else:
                color, label = "#ff6b6b", "HIGH (> 0.99)"
            st.markdown(f'<span style="color:{color};font-weight:bold;font-size:1.3em">{p:.3f}</span> <span style="color:{color}">{label}</span>', unsafe_allow_html=True)
            st.progress(min(p, 1.0))
            st.caption(f"Delta: {sig.get('delta',0):.3f}% | Conf: {sig.get('conf',0)}%")
        else:
            st.caption("No data")

# ===== SKIP REASONS =====
if stats['skip_reasons']:
    st.divider()
    st.subheader("Skip Reasons")
    total = stats['skips']
    for reason, cnt in sorted(stats['skip_reasons'].items(), key=lambda x: -x[1]):
        label = SKIP_LABELS.get(reason, reason)
        pct = cnt / max(total, 1)
        st.progress(pct, text=f"{label}: {cnt} ({pct*100:.0f}%)")

# ===== RECENT SIGNALS =====
if stats['last_signals']:
    st.divider()
    st.subheader(f"Signals ({len(stats['last_signals'])})")
    rows = []
    for s in reversed(stats['last_signals']):
        rows.append({
            "Coin": s.get('coin','?'),
            "Side": s.get('pm_side','?'),
            "PM": f"{s.get('pm_price',0):.3f}",
            "Delta%": f"{s.get('delta',0):.3f}",
            "Conf%": s.get('conf',0),
            "Time": f"{s.get('time',0):.0f}s",
            "BTC": f"${s.get('binance_price',0):.0f}" if s.get('coin')=='BTC' else "",
            "ETH": f"${s.get('binance_price',0):.0f}" if s.get('coin')=='ETH' else "",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

# ===== RECENT DECISIONS =====
if stats['last_skips']:
    st.divider()
    st.subheader(f"Recent Skips ({len(stats['last_skips'])})")
    for s in reversed(stats['last_skips'][-10:]):
        label = SKIP_LABELS.get(s.get('reason',''), s.get('reason',''))
        st.markdown(f'<div class="skip-card">🔴 [{s.get("coin","?")}] SKIP — {label}</div>', unsafe_allow_html=True)

if stats['last_trades']:
    st.divider()
    st.subheader(f"Recent Trades ({len(stats['last_trades'])})")
    for s in reversed(stats['last_trades'][-10:]):
        st.markdown(f'<div class="trade-card">🟢 [{s.get("coin","?")}] TRADE — +${s.get("pnl",0):.2f} expected</div>', unsafe_allow_html=True)

# ===== FOOTER =====
st.divider()
st.caption(f"5m Poly Bot Dashboard | {'Auto-refresh ON' if auto_refresh else 'Auto-refresh OFF'} | {stats['last_update']}")

# ===== AUTO-REFRESH (at the very end, after all rendering) =====
if auto_refresh:
    time.sleep(3)
    st.rerun()
