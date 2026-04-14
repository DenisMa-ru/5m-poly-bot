"""
Streamlit Dashboard for 5m-poly-bot
One-screen layout — no scrolling needed.
Run: streamlit run dashboard.py --server.port 3001 --server.address 0.0.0.0 --server.headless true
"""
import streamlit as st
import re
import os
from datetime import datetime
from pathlib import Path
import time

DEFAULT_LOG = Path("/root/5m-poly-bot/bot.log")

st.set_page_config(page_title="5m Poly Bot", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

# ===== CSS =====
st.markdown("""
<style>
    /* Compact metrics */
    [data-testid="stMetricValue"] { font-size: 1.4em !important; }
    [data-testid="stMetricLabel"] { font-size: 0.75em !important; color: #888; }
    /* Compact tables */
    .stDataFrame { font-size: 0.8em; }
    /* Cards */
    .skip-row { display: flex; gap: 8px; flex-wrap: wrap; }
    .skip-chip { background: #2a1a1a; border-left: 3px solid #ff6b6b; padding: 4px 10px;
                  border-radius: 4px; font-size: 0.8em; white-space: nowrap; }
    .trade-chip { background: #1a2a1a; border-left: 3px solid #51cf66; padding: 4px 10px;
                  border-radius: 4px; font-size: 0.8em; white-space: nowrap; }
    /* Hide default streamlit padding */
    .block-container { padding-top: 1rem; padding-bottom: 1rem; }
    /* Footer */
    .footer { position: fixed; bottom: 0; left: 0; right: 0; background: #0e1117;
              padding: 4px 20px; font-size: 0.7em; color: #555; text-align: center; z-index: 999; }
</style>
""", unsafe_allow_html=True)

# ===== PATTERNS =====
_P = {
    "ts":       re.compile(r'\[(\d{4}-\d{2}-\d{2}T[\d:]+Z?)\]'),
    "active":   re.compile(r'Active window.*?close\s+([\d:]+)'),
    "sleep":    re.compile(r'Sleeping\s+(\d+)s'),
    "sleep_nc": re.compile(r'next close\s+([\d:]+)'),
    "closed":   re.compile(r'Market closed'),
    "coin":     re.compile(r'\[(BTC|ETH)\]'),
    "time":     re.compile(r'(\d+\.?\d*)s\s*\|'),
    "pm":       re.compile(r'PM:(Up|Down)@([\d.]+)'),
    "delta":    re.compile(r'delta:([\d.]+)%'),
    "conf":     re.compile(r'conf:(\d+)%'),
    "price":    re.compile(r'Price:([\d,.]+)'),
    "momentum": re.compile(r'momentum=(↑|↓)'),
    "atr":      re.compile(r'range\s+\$?([\d.]+)\s*>\s*1\.5x\s*ATR\s+\$?([\d.]+)'),
    "skip":     re.compile(r'\[(BTC|ETH)\].*SKIP'),
    "err":      re.compile(r'BINANCE ERROR'),
    # FIXED: match ENTERING [ETH Up] — bracket before side word
    "enter":    re.compile(r'ENTERING \[(BTC|ETH)\s+\w+\]'),
    "amt":      re.compile(r'invested=\$([\d.]+)'),
    "pnl":      re.compile(r'expected_pnl=\+?\$([\d.]+)'),
}


def parse(line):
    if not line.strip():
        return None
    r = {'raw': line}
    m = _P["ts"].search(line)
    if m:
        r['ts'] = m.group(1)
    if _P["active"].search(line):
        r['type'] = 'active'
        m2 = _P["active"].search(line)
        if m2: r['nc'] = m2.group(1)
        return r
    if _P["sleep"].search(line):
        r['type'] = 'sleeping'
        m2 = _P["sleep"].search(line)
        if m2: r['sec'] = int(m2.group(1))
        m3 = _P["sleep_nc"].search(line)
        if m3: r['nc'] = m3.group(1)
        return r
    if _P["closed"].search(line):
        r['type'] = 'closed'
        return r
    if '🎯' in line and ('PM:Up@' in line or 'PM:Down@' in line):
        r['type'] = 'signal'
        m2 = _P["coin"].search(line)
        if m2: r['coin'] = m2.group(1)
        m2 = _P["time"].search(line)
        if m2: r['t'] = float(m2.group(1))
        m2 = _P["pm"].search(line)
        if m2:
            r['side'] = m2.group(1)
            r['pm'] = float(m2.group(2))
        m2 = _P["delta"].search(line)
        if m2: r['d'] = float(m2.group(1))
        m2 = _P["conf"].search(line)
        if m2: r['c'] = int(m2.group(1))
        m2 = _P["price"].search(line)
        if m2: r['bp'] = float(m2.group(1).replace(',', ''))
        m2 = _P["momentum"].search(line)
        if m2: r['mom'] = m2.group(1)
        m2 = _P["atr"].search(line)
        if m2: r['atr'] = float(m2.group(2))
        return r
    if 'SKIP' in line:
        r['type'] = 'skip'
        m2 = _P["coin"].search(line)
        if m2: r['coin'] = m2.group(1)
        raw = line
        if '< 0.94' in raw: r['reason'] = 'btc_low'
        elif '< 0.92' in raw: r['reason'] = 'eth_low'
        elif '> 0.99' in raw: r['reason'] = 'high'
        elif 'too close' in raw: r['reason'] = 'delta'
        elif 'confidence' in raw and '< 30%' in raw: r['reason'] = 'conf'
        elif 'ATR skip' in raw: r['reason'] = 'atr'
        else: r['reason'] = 'other'
        return r
    if _P["err"].search(line):
        r['type'] = 'err'
        return r
    if _P["enter"].search(line):
        r['type'] = 'trade'
        m2 = _P["enter"].search(line)
        if m2: r['coin'] = m2.group(1)
        m2 = _P["amt"].search(line)
        if m2: r['amt'] = float(m2.group(1))
        m2 = _P["pnl"].search(line)
        if m2: r['pnl'] = float(m2.group(1))
        return r
    return None


def parse_logs(path, n=3000):
    if not path or not path.exists():
        return None, None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None, None

    evts = []
    for l in lines[-n:]:
        e = parse(l)
        if e:
            evts.append(e)

    s = {
        'signals': 0, 'skips': 0, 'trades': 0, 'rounds': 0, 'errs': 0,
        'state': 'starting', 'sleep': 0, 'nc': '--:--',
        'btc_p': None, 'eth_p': None, 'btc_a': None, 'eth_a': None,
        'invested': 0.0, 'pnl': 0.0,
        'reasons': {}, 'last_sig': [], 'last_skip': [], 'last_trade': [],
        'time': datetime.now().strftime('%H:%M:%S'),
        'n_lines': len(lines),
    }

    for e in evts:
        t = e['type']
        if t == 'signal':
            s['signals'] += 1
            s['last_sig'].append(e)
            if e.get('coin') == 'BTC' and e.get('bp'): s['btc_p'] = e['bp']
            if e.get('coin') == 'ETH' and e.get('bp'): s['eth_p'] = e['bp']
            if e.get('atr'):
                if e.get('coin') == 'BTC': s['btc_a'] = e['atr']
                else: s['eth_a'] = e['atr']
        elif t == 'skip':
            s['skips'] += 1
            s['last_skip'].append(e)
            rr = e.get('reason', 'other')
            s['reasons'][rr] = s['reasons'].get(rr, 0) + 1
        elif t == 'trade':
            s['trades'] += 1
            s['last_trade'].append(e)
            s['invested'] += e.get('amt', 0)
            s['pnl'] += e.get('pnl', 0)
        elif t == 'active':
            s['state'] = 'active'
            s['nc'] = e.get('nc', '--:--')
        elif t == 'sleeping':
            s['state'] = 'sleeping'
            s['sleep'] = e.get('sec', 0)
            s['nc'] = e.get('nc', '--:--')
        elif t == 'closed':
            s['rounds'] += 1
        elif t == 'err':
            s['errs'] += 1

    s['last_sig'] = s['last_sig'][-15:]
    s['last_skip'] = s['last_skip'][-15:]
    s['last_trade'] = s['last_trade'][-10:]
    return lines, s


RL = {
    'btc_low': 'BTC < 0.94', 'eth_low': 'ETH < 0.92', 'high': '> 0.99',
    'delta': 'delta < 0.05%', 'conf': 'conf < 30%', 'atr': 'ATR high', 'other': '?'
}

# ===== SIDEBAR =====
log_path = Path(os.environ.get("BOT_LOG_FILE", str(DEFAULT_LOG)))
st.sidebar.caption(f"📁 `{log_path.name}`")

# ===== PARSE =====
_, S = parse_logs(log_path)
if not S:
    st.error(f"Cannot read `{log_path}`")
    st.stop()

st.sidebar.caption(f"Lines: {S['n_lines']} | {S['time']}")

# ===== AUTO-REFRESH =====
ar = st.sidebar.checkbox("↻ Auto-refresh", value=True)

# ===== ROW 1: STATUS BAR =====
ico = {'sleeping': '💤', 'active': '⚡', 'starting': '🚀'}.get(S['state'], '⚪')
a1, a2, a3, a4, a5, a6, a7, a8 = st.columns(8)
a1.metric("Bot", f"{ico} {S['state'].upper()}")
a2.metric("Next Close", S['nc'])
a3.metric("Sleep", f"{S['sleep']}s")
a4.metric("Signals", S['signals'])
a5.metric("Rounds", S['rounds'])
a6.metric("Trades", S['trades'])
a7.metric("Binance", f"❌{S['errs']}" if S['errs'] else "✅ OK")

# ===== ROW 2: PnL + PRICES =====
b1, b2, b3, b4, b5 = st.columns(5)
with b1:
    st.metric("Invested", f"${S['invested']:.1f}")
with b2:
    pnl_val = S['pnl']
    pnl_color = "🟢" if pnl_val >= 0 else "🔴"
    st.metric("Expected PnL", f"{pnl_color} ${pnl_val:+.2f}")
with b3:
    st.metric("BTC", f"${S['btc_p']:.0f}" if S['btc_p'] else "—")
with b4:
    st.metric("ETH", f"${S['eth_p']:.0f}" if S['eth_p'] else "—")
with b5:
    skip_rate = S['skips'] / max(S['skips'] + S['trades'], 1) * 100
    st.metric("Skip Rate", f"{skip_rate:.0f}%")

# ===== ROW 3: PM PRICES + SKIP REASONS =====
c1, c2 = st.columns([1, 1])
with c1:
    st.markdown("**Polymarket Prices**")
    btc_s = next((x for x in reversed(S['last_sig']) if x.get('coin') == 'BTC'), None)
    eth_s = next((x for x in reversed(S['last_sig']) if x.get('coin') == 'ETH'), None)
    for sig, name, mn in [(btc_s, "BTC", 0.94), (eth_s, "ETH", 0.92)]:
        if sig and sig.get('pm'):
            p = sig['pm']
            ok = mn <= p <= 0.99
            col = "#51cf66" if ok else "#ff6b6b"
            lbl = "GOOD" if ok else ("LOW" if p < mn else "HIGH")
            st.markdown(f'`{name}` <span style="color:{col};font-weight:bold">{p:.3f} {lbl}</span> '
                        f'<span style="color:#888">Δ{sig.get("d",0):.3f}% C{sig.get("c",0)}%</span>',
                        unsafe_allow_html=True)
        else:
            st.markdown(f"`{name}` —")
with c2:
    if S['reasons']:
        st.markdown("**Skip Reasons**")
        tot = S['skips']
        for rr, cnt in sorted(S['reasons'].items(), key=lambda x: -x[1]):
            pct = cnt / max(tot, 1)
            lbl = RL.get(rr, rr)
            st.progress(pct, text=f"{lbl}: {cnt} ({pct*100:.0f}%)")
    else:
        st.caption("No skips yet")

# ===== ROW 4: SIGNALS TABLE =====
if S['last_sig']:
    st.markdown("---")
    st.markdown("**Last Signals**")
    rows = []
    for x in reversed(S['last_sig']):
        rows.append({
            "Coin": x.get('coin',''),
            "Side": x.get('side',''),
            "PM": f"{x.get('pm',0):.3f}",
            "Δ%": f"{x.get('d',0):.3f}",
            "Conf%": x.get('c',0),
            "Time": f"{x.get('t',0):.0f}s",
            "Binance": f"${x.get('bp',0):.0f}" if x.get('bp') else "",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True, height=180)

# ===== ROW 5: RECENT TRADES + SKIPS =====
if S['last_trade'] or S['last_skip']:
    st.markdown("---")
    d1, d2 = st.columns([1, 1])
    with d1:
        if S['last_trade']:
            st.markdown("**Trades**")
            for x in reversed(S['last_trade'][-5:]):
                st.markdown(f'<div class="trade-chip">{x.get("coin","")} +${x.get("pnl",0):.2f}</div>',
                            unsafe_allow_html=True)
    with d2:
        if S['last_skip']:
            st.markdown("**Recent Skips**")
            chips = []
            for x in reversed(S['last_skip'][-8:]):
                lbl = RL.get(x.get('reason',''), '?')
                chips.append(f'<span class="skip-chip">{x.get("coin","")} {lbl}</span>')
            st.markdown(f'<div class="skip-row">{"".join(chips)}</div>', unsafe_allow_html=True)

# ===== FOOTER =====
st.markdown("---")
st.caption(f"5m Poly Bot | {'↻ ON' if ar else '↻ OFF'} | {S['time']} | {S['n_lines']} lines")

if ar:
    time.sleep(4)
    st.rerun()
