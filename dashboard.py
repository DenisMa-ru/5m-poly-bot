"""
5m Poly Bot Dashboard v2 — Управление ботом, статистика, настройки, сохранение сигналов.
Запуск: streamlit run dashboard.py --server.port 3001 --server.address 0.0.0.0 --server.headless true
"""
import streamlit as st
import re, os, json, subprocess, signal, time
from datetime import datetime
from pathlib import Path

# ===== CONFIG =====
DEFAULT_LOG = Path("/root/5m-poly-bot/bot.log")
CONTROL_FILE = Path("/root/5m-poly-bot/control.json")
SIGNALS_FILE = Path("/root/5m-poly-bot/signals.json")
BOT_DIR = Path("/root/5m-poly-bot")
BOT_SCRIPT = "crypto_bot.py"
PID_FILE = Path("/root/5m-poly-bot/bot.pid")

st.set_page_config(page_title="5m Poly Bot", page_icon="📈", layout="wide",
                    initial_sidebar_state="collapsed")

# ===== CSS =====
st.markdown("""
<style>
    #MainMenu, footer, header { visibility: hidden; }
    [data-testid="stToolbar"] { display: none !important; }
    [data-testid="stSidebar"] { display: none; }
    [data-testid="stMetricLabel"] { font-size: 0.7em !important; color: #8b949e !important; text-transform: uppercase; }
    [data-testid="stMetricValue"] { font-size: 1.3em !important; }
    .block-container { padding-top: 0.5rem !important; padding-bottom: 0.3rem !important; }
    hr { margin: 6px 0 !important; border-color: #21262d !important; }
    .trade-chip { display:inline-block; background:#1a2a1a; border-left:3px solid #51cf66;
                   padding:3px 8px; border-radius:3px; font-size:0.85em; color:#51cf66; margin:3px; white-space: nowrap; }
    .skip-chip { display:inline-block; background:#2a1a1a; border-left:3px solid #ff6b6b;
                  padding:3px 8px; border-radius:3px; font-size:0.85em; color:#ff8888; margin:3px; white-space: nowrap; }
    .status-running { color: #51cf66; font-weight: bold; }
    .status-stopped { color: #ff6b6b; font-weight: bold; }
    .pm-ok { color: #51cf66; font-weight: bold; }
    .pm-bad { color: #ff6b6b; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# ===== PATTERNS =====
_P = {
    "active": re.compile(r'Active window.*?close\s+([\d:]+)'),
    "sleep":  re.compile(r'Sleeping\s+(\d+)s'),
    "snc":    re.compile(r'next close\s+([\d:]+)'),
    "closed": re.compile(r'Market closed'),
    "coin":   re.compile(r'\[(BTC|ETH)\]'),
    "time":   re.compile(r'(\d+\.?\d*)s\s*\|'),
    "pm":     re.compile(r'PM:(Up|Down)@([\d.]+)'),
    "delta":  re.compile(r'delta:([\d.]+)%'),
    "conf":   re.compile(r'conf:(\d+)%'),
    "price":  re.compile(r'Price:([\d,.]+)'),
    "atr":    re.compile(r'range\s+\$?([\d.]+)\s*>\s*1\.5x\s*ATR\s+\$?([\d.]+)'),
    "enter":  re.compile(r'ENTERING \[(BTC|ETH)\s+\w+\]'),
    "amt":    re.compile(r'invested=\$([\d.]+)'),
    "pnl":    re.compile(r'expected_pnl=\+?\$([\d.]+)'),
}

def parse(line):
    if not line.strip():
        return None
    r = {'raw': line}
    if _P["active"].search(line):
        r['type'] = 'active'
        m = _P["active"].search(line)
        if m: r['nc'] = m.group(1)
        return r
    if _P["sleep"].search(line):
        r['type'] = 'sleeping'
        m = _P["sleep"].search(line)
        if m: r['sec'] = int(m.group(1))
        m3 = _P["snc"].search(line)
        if m3: r['nc'] = m3.group(1)
        return r
    if _P["closed"].search(line):
        r['type'] = 'closed'
        return r
    if '\U0001f3af' in line and ('PM:Up@' in line or 'PM:Down@' in line):
        r['type'] = 'signal'
        m = _P["coin"].search(line)
        if m: r['coin'] = m.group(1)
        m = _P["time"].search(line)
        if m: r['t'] = float(m.group(1))
        m = _P["pm"].search(line)
        if m: r['side'] = m.group(1); r['pm'] = float(m.group(2))
        m = _P["delta"].search(line)
        if m: r['d'] = float(m.group(1))
        m = _P["conf"].search(line)
        if m: r['c'] = int(m.group(1))
        m = _P["price"].search(line)
        if m: r['bp'] = float(m.group(1).replace(',', ''))
        return r
    if 'SKIP' in line:
        r['type'] = 'skip'
        m = _P["coin"].search(line)
        if m: r['coin'] = m.group(1)
        raw = line
        if '< 0.94' in raw: r['r'] = 'btc_low'
        elif '< 0.92' in raw: r['r'] = 'eth_low'
        elif '> 0.99' in raw: r['r'] = 'high'
        elif 'too close' in raw: r['r'] = 'delta'
        elif 'confidence' in raw and '< 30%' in raw: r['r'] = 'conf'
        elif 'ATR skip' in raw: r['r'] = 'atr'
        else: r['r'] = 'other'
        return r
    if re.compile(r'BINANCE ERROR').search(line):
        r['type'] = 'err'
        return r
    if _P["enter"].search(line):
        r['type'] = 'trade'
        m = _P["enter"].search(line)
        if m: r['coin'] = m.group(1)
        m = _P["amt"].search(line)
        if m: r['amt'] = float(m.group(1))
        m = _P["pnl"].search(line)
        if m: r['pnl'] = float(m.group(1))
        return r
    return None

def parse_logs(path, n=3000):
    if not path or not path.exists():
        return None, None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except:
        return None, None
    evts = [e for l in lines[-n:] if (e := parse(l)) is not None]
    s = {'sig': 0, 'skip': 0, 'trade': 0, 'round': 0, 'err': 0, 'state': 'start',
         'sleep': 0, 'nc': '--:--', 'btc_p': None, 'eth_p': None,
         'invested': 0, 'pnl': 0, 'reasons': {}, 'last_sig': [], 'last_skip': [], 'last_trade': [],
         'time': datetime.now().strftime('%H:%M:%S'), 'n': len(lines)}
    for e in evts:
        t = e['type']
        if t == 'signal':
            s['sig'] += 1; s['last_sig'].append(e)
            if e.get('coin') == 'BTC' and e.get('bp'): s['btc_p'] = e['bp']
            if e.get('coin') == 'ETH' and e.get('bp'): s['eth_p'] = e['bp']
        elif t == 'skip':
            s['skip'] += 1; s['last_skip'].append(e)
            rr = e.get('r', 'other'); s['reasons'][rr] = s['reasons'].get(rr, 0) + 1
        elif t == 'trade':
            s['trade'] += 1; s['last_trade'].append(e)
            s['invested'] += e.get('amt', 0); s['pnl'] += e.get('pnl', 0)
        elif t == 'active': s['state'] = 'active'; s['nc'] = e.get('nc', '--:--')
        elif t == 'sleeping': s['state'] = 'sleeping'; s['sleep'] = e.get('sec', 0); s['nc'] = e.get('nc', '--:--')
        elif t == 'closed': s['round'] += 1
        elif t == 'err': s['err'] += 1
    s['last_sig'] = s['last_sig'][-12:]
    s['last_skip'] = s['last_skip'][-12:]
    s['last_trade'] = s['last_trade'][-12:]
    return lines, s

RL = {'btc_low': 'BTC<0.94', 'eth_low': 'ETH<0.92', 'high': '>0.99',
      'delta': 'δ<0.05%', 'conf': 'conf<30%', 'atr': 'ATR↑', 'other': '?'}

# ===== BOT CONTROL HELPERS =====
def write_control(cmd, mode=None, amount=None, settings=None):
    """Write command to control file for the bot to read."""
    data = {"cmd": cmd, "timestamp": datetime.now().isoformat()}
    if mode: data["mode"] = mode
    if amount: data["amount"] = amount
    if settings: data["settings"] = settings
    try:
        CONTROL_FILE.write_text(json.dumps(data))
    except: pass

def get_control():
    try:
        return json.loads(CONTROL_FILE.read_text())
    except:
        return {}

def is_bot_running():
    """Check if bot process is running via PID file."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except:
        return False

def start_bot(mode="dry-run", amount=10):
    """Start the bot as a background process."""
    if is_bot_running():
        return False
    try:
        log_file = open(str(BOT_DIR / "bot.log"), "a")
        proc = subprocess.Popen(
            ["python3", str(BOT_SCRIPT), f"--{mode}", "--amount", str(amount)],
            cwd=str(BOT_DIR),
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        return True
    except:
        return False

def stop_bot():
    """Stop the bot process."""
    if not PID_FILE.exists():
        return True
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
        try:
            os.kill(pid, signal.SIGKILL)
        except: pass
        PID_FILE.unlink(missing_ok=True)
        return True
    except:
        return False

def restart_bot(mode="dry-run", amount=10):
    stop_bot()
    time.sleep(1)
    return start_bot(mode, amount)

def load_saved_signals():
    """Load all saved signals from signals.json."""
    try:
        data = json.loads(SIGNALS_FILE.read_text())
        return data if isinstance(data, list) else []
    except:
        return []

def save_signal(signal_data):
    """Append a signal to signals.json."""
    signals = load_saved_signals()
    signals.append(signal_data)
    # Keep last 10000 signals
    signals = signals[-10000:]
    try:
        SIGNALS_FILE.write_text(json.dumps(signals, indent=2))
    except: pass

def load_settings():
    """Load bot settings."""
    try:
        return json.loads((BOT_DIR / "settings.json").read_text())
    except:
        return {
            "mode": "dry-run",
            "amount": 10,
            "min_confidence": 0.3,
            "entry_min": 10,
            "entry_max": 50,
            "price_min_btc": 0.94,
            "price_min_eth": 0.92,
            "price_max": 0.99,
            "delta_skip": 0.0005,
            "atr_multiplier": 1.5,
        }

def save_settings(settings):
    try:
        (BOT_DIR / "settings.json").write_text(json.dumps(settings, indent=2))
    except: pass

# ===== INIT =====
log_path = Path(os.environ.get("BOT_LOG_FILE", str(DEFAULT_LOG)))
_, S = parse_logs(log_path)
if not S:
    st.error(f"Cannot read `{log_path}`")
    st.stop()

settings = load_settings()
bot_running = is_bot_running()

# ===== TABS =====
tab_dashboard, tab_history, tab_stats, tab_settings = st.tabs(["📊 Dashboard", "📋 History", "📈 Statistics", "⚙️ Settings"])

# ==========================================
# TAB 1: DASHBOARD
# ==========================================
with tab_dashboard:
    # ---- CONTROL BAR ----
    st.markdown("### 🎮 Bot Control")
    ctrl_cols = st.columns([1, 1, 1, 1, 2, 1])

    with ctrl_cols[0]:
        if not bot_running:
            if st.button("▶️ Start", use_container_width=True, type="primary"):
                start_bot(settings.get("mode", "dry-run"), settings.get("amount", 10))
                st.rerun()
        else:
            if st.button("▶️ Running", use_container_width=True, disabled=True):
                pass

    with ctrl_cols[1]:
        if bot_running:
            if st.button("⏹️ Stop", use_container_width=True):
                stop_bot()
                st.rerun()
        else:
            if st.button("⏹️ Stopped", use_container_width=True, disabled=True):
                pass

    with ctrl_cols[2]:
        if st.button("🔄 Restart", use_container_width=True):
            restart_bot(settings.get("mode", "dry-run"), settings.get("amount", 10))
            st.rerun()

    with ctrl_cols[3]:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    status_text = f"{'🟢 Running' if bot_running else '🔴 Stopped'}"
    ctrl_cols[4].markdown(f"**Status:** {status_text}  |  Mode: `{settings.get('mode', 'dry-run')}`  |  Amount: `${settings.get('amount', 10)}`")

    ctrl_cols[5].caption(f"PID: {PID_FILE.read_text().strip() if PID_FILE.exists() else '—'}")

    st.markdown("---")

    # ---- STATUS BAR ----
    ico = {'sleeping': '💤', 'active': '⚡', 'start': '🚀'}.get(S['state'], '⚪')
    st.markdown(f"### {ico} {S['state'].upper()} — Next: {S['nc']} | Sleep: {S['sleep']}s")

    a1, a2, a3, a4, a5, a6 = st.columns(6)
    a1.metric("Signals", S['sig'])
    a2.metric("Rounds", S['round'])
    a3.metric("Trades", S['trade'])
    a4.metric("Binance", "❌ Err" if S['err'] else "✅ OK")
    a5.metric("Skip Rate", f"{S['skip']/max(S['sig'],1)*100:.0f}%")
    a6.metric("Updated", S['time'])

    # ---- MONEY + PRICES ----
    st.markdown("---")
    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Invested", f"${S['invested']:.1f}")
    pnl_v = S['pnl']
    b2.metric("Expected PnL", f"${pnl_v:+.2f}",
               delta=f"{pnl_v:+.2f}",
               delta_color="normal" if pnl_v >= 0 else "inverse")
    b3.metric("BTC", f"${S['btc_p']:.0f}" if S['btc_p'] else "—")
    b4.metric("ETH", f"${S['eth_p']:.0f}" if S['eth_p'] else "—")

    # ---- PM PRICES + SKIP REASONS ----
    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Polymarket**")
        for sig, name, mn in [
            (next((x for x in reversed(S['last_sig']) if x.get('coin') == 'BTC'), None), "BTC", 0.94),
            (next((x for x in reversed(S['last_sig']) if x.get('coin') == 'ETH'), None), "ETH", 0.92),
        ]:
            if sig and sig.get('pm'):
                p = sig['pm']
                ok = mn <= p <= 0.99
                col = "#51cf66" if ok else "#ff6b6b"
                lbl = "✓" if ok else "✗"
                st.markdown(f'`{name}` <span style="color:{col};font-weight:bold">{p:.3f} {lbl}</span> '
                            f'δ{sig.get("d", 0):.2f}% C{sig.get("c", 0)}%', unsafe_allow_html=True)
            else:
                st.markdown(f"`{name}` —")
    with c2:
        if S['reasons']:
            st.markdown("**Why skipping**")
            tot = S['skip']
            for rr, cnt in sorted(S['reasons'].items(), key=lambda x: -x[1]):
                pct = cnt / max(tot, 1)
                st.progress(pct, text=f"{RL.get(rr, rr)}: {cnt} ({pct*100:.0f}%)")
        else:
            st.caption("No skips")

    # ---- TRADES + SKIP CHIPS (FIXED HTML) ----
    if S['last_trade'] or S['last_skip']:
        st.markdown("---")
        d1, d2 = st.columns(2)
        with d1:
            if S['last_trade']:
                st.markdown("**Trades**")
                # FIX: Use st.html for proper rendering
                trade_html = '<div style="display:flex;flex-wrap:wrap;gap:4px;">'
                for x in reversed(S['last_trade'][-8:]):
                    trade_html += f'<span class="trade-chip">{x.get("coin", "")} +${x.get("pnl", 0):.2f}</span>'
                trade_html += '</div>'
                st.markdown(trade_html, unsafe_allow_html=True)
        with d2:
            if S['last_skip']:
                st.markdown("**Last skips**")
                skip_html = '<div style="display:flex;flex-wrap:wrap;gap:4px;">'
                for x in reversed(S['last_skip'][-12:]):
                    skip_html += f'<span class="skip-chip">{x.get("coin", "")} {RL.get(x.get("r", ""), "?")}</span>'
                skip_html += '</div>'
                st.markdown(skip_html, unsafe_allow_html=True)

    # ---- SIGNALS TABLE ----
    if S['last_sig']:
        with st.expander(f"📋 Last Signals ({len(S['last_sig'])})", expanded=False):
            rows = []
            for x in reversed(S['last_sig']):
                rows.append({
                    "Coin": x.get('coin', ''),
                    "Side": x.get('side', ''),
                    "PM": f"{x.get('pm', 0):.3f}",
                    "Δ%": f"{x.get('d', 0):.3f}",
                    "Conf%": x.get('c', 0),
                    "Time": f"{x.get('t', 0):.0f}s",
                })
            st.dataframe(rows, use_container_width=True, hide_index=True, height=160)

# ==========================================
# TAB 2: HISTORY
# ==========================================
with tab_history:
    st.markdown("### 📋 All Signals History")

    all_signals = load_saved_signals()

    if all_signals:
        st.caption(f"Total signals saved: {len(all_signals)}")

        # Filter controls
        f1, f2, f3 = st.columns(3)
        with f1:
            coin_filter = st.selectbox("Coin", ["All", "BTC", "ETH"], key="hf_coin")
        with f2:
            side_filter = st.selectbox("Side", ["All", "Up", "Down"], key="hf_side")
        with f3:
            show_count = st.slider("Show last N", 10, 500, 50, key="hf_count")

        filtered = all_signals
        if coin_filter != "All":
            filtered = [s for s in filtered if s.get("coin") == coin_filter]
        if side_filter != "All":
            filtered = [s for s in filtered if s.get("side") == side_filter]
        filtered = filtered[-show_count:]

        if filtered:
            rows = []
            for x in reversed(filtered):
                rows.append({
                    "Time": x.get("timestamp", ""),
                    "Coin": x.get("coin", ""),
                    "Side": x.get("side", ""),
                    "PM Price": f"{x.get('pm', 0):.3f}",
                    "Delta %": f"{x.get('delta', 0):.3f}",
                    "Confidence": f"{x.get('confidence', 0):.0%}",
                    "Price": f"{x.get('price', 0):.0f}",
                    "Seconds Left": f"{x.get('time_left', 0):.0f}s",
                    "Entered": "✅" if x.get("entered") else "❌",
                    "Reason": x.get("reason", "")[:50],
                })
            st.dataframe(rows, use_container_width=True, hide_index=True, height=400)
        else:
            st.info("No signals match filter")

        if st.button("🗑️ Clear History"):
            SIGNALS_FILE.write_text("[]")
            st.rerun()
    else:
        st.info("No saved signals yet. Signals are saved when the bot processes them.")

# ==========================================
# TAB 3: STATISTICS
# ==========================================
with tab_stats:
    st.markdown("### 📈 Statistics")

    all_signals = load_saved_signals()

    if all_signals:
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Total Signals", len(all_signals))

        btc_signals = [x for x in all_signals if x.get("coin") == "BTC"]
        eth_signals = [x for x in all_signals if x.get("coin") == "ETH"]
        s2.metric("BTC Signals", len(btc_signals))
        s3.metric("ETH Signals", len(eth_signals))

        entered = [x for x in all_signals if x.get("entered")]
        s4.metric("Entries", len(entered))

        st.markdown("---")

        # Win rate by coin
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**BTC Stats**")
            btc_entered = [x for x in btc_signals if x.get("entered")]
            if btc_entered:
                st.metric("Entries", len(btc_entered))
                avg_conf = sum(x.get("confidence", 0) for x in btc_entered) / len(btc_entered)
                st.metric("Avg Confidence", f"{avg_conf:.0%}")
                avg_delta = sum(x.get("delta", 0) for x in btc_entered) / len(btc_entered)
                st.metric("Avg Delta", f"{avg_delta:.4f}%")
            else:
                st.caption("No BTC entries yet")

        with col2:
            st.markdown("**ETH Stats**")
            eth_entered = [x for x in eth_signals if x.get("entered")]
            if eth_entered:
                st.metric("Entries", len(eth_entered))
                avg_conf = sum(x.get("confidence", 0) for x in eth_entered) / len(eth_entered)
                st.metric("Avg Confidence", f"{avg_conf:.0%}")
                avg_delta = sum(x.get("delta", 0) for x in eth_entered) / len(eth_entered)
                st.metric("Avg Delta", f"{avg_delta:.4f}%")
            else:
                st.caption("No ETH entries yet")

        st.markdown("---")

        # Skip reasons breakdown
        st.markdown("**Current Session Skip Reasons**")
        if S['reasons']:
            tot = S['skip']
            for rr, cnt in sorted(S['reasons'].items(), key=lambda x: -x[1]):
                pct = cnt / max(tot, 1)
                st.progress(pct, text=f"{RL.get(rr, rr)}: {cnt} ({pct*100:.0f}%)")
        else:
            st.caption("No skip data")

        if st.button("🗑️ Reset Statistics"):
            SIGNALS_FILE.write_text("[]")
            st.rerun()
    else:
        st.info("No statistics yet. Statistics are built from saved signals.")

# ==========================================
# TAB 4: SETTINGS
# ==========================================
with tab_settings:
    st.markdown("### ⚙️ Bot Settings")

    new_settings = settings.copy()

    s1, s2 = st.columns(2)
    with s1:
        st.markdown("**Run Configuration**")
        new_settings["mode"] = st.selectbox(
            "Mode",
            ["dry-run", "paper", "live"],
            index=["dry-run", "paper", "live"].index(settings.get("mode", "dry-run")),
            help="dry-run: real data no trades | paper: simulated | live: real funds"
        )
        new_settings["amount"] = st.number_input(
            "Amount per trade (USDC)",
            min_value=1.0, max_value=1000.0,
            value=float(settings.get("amount", 10)),
            step=1.0
        )

    with s2:
        st.markdown("**Entry Window**")
        new_settings["entry_min"] = st.number_input(
            "Entry window min (seconds before close)",
            min_value=1, max_value=120,
            value=int(settings.get("entry_min", 10)),
            step=1
        )
        new_settings["entry_max"] = st.number_input(
            "Entry window max (seconds before close)",
            min_value=5, max_value=300,
            value=int(settings.get("entry_max", 50)),
            step=5
        )

    st.markdown("---")
    s3, s4 = st.columns(2)
    with s3:
        st.markdown("**Price Thresholds**")
        new_settings["price_min_btc"] = st.number_input(
            "BTC min price",
            min_value=0.50, max_value=1.0,
            value=float(settings.get("price_min_btc", 0.94)),
            step=0.01, format="%.2f"
        )
        new_settings["price_min_eth"] = st.number_input(
            "ETH min price",
            min_value=0.50, max_value=1.0,
            value=float(settings.get("price_min_eth", 0.92)),
            step=0.01, format="%.2f"
        )
        new_settings["price_max"] = st.number_input(
            "Max price (both)",
            min_value=0.90, max_value=1.0,
            value=float(settings.get("price_max", 0.99)),
            step=0.01, format="%.2f"
        )

    with s4:
        st.markdown("**Signal Thresholds**")
        new_settings["min_confidence"] = st.slider(
            "Min confidence",
            min_value=0.0, max_value=1.0,
            value=float(settings.get("min_confidence", 0.3)),
            step=0.05
        )
        new_settings["delta_skip"] = st.number_input(
            "Delta skip (%)",
            min_value=0.0001, max_value=0.01,
            value=float(settings.get("delta_skip", 0.0005)),
            step=0.0001, format="%.4f"
        )
        new_settings["atr_multiplier"] = st.number_input(
            "ATR multiplier",
            min_value=0.5, max_value=5.0,
            value=float(settings.get("atr_multiplier", 1.5)),
            step=0.1
        )

    st.markdown("---")
    btn_cols = st.columns([1, 1, 3])
    with btn_cols[0]:
        if st.button("💾 Save Settings", type="primary", use_container_width=True):
            save_settings(new_settings)
            st.success("Settings saved!")
            st.rerun()
    with btn_cols[1]:
        if st.button("🔄 Save & Restart Bot", use_container_width=True):
            save_settings(new_settings)
            restart_bot(new_settings["mode"], new_settings["amount"])
            st.success("Saved & restarted!")
            st.rerun()

    st.caption("Changes are saved to `settings.json`. The bot reads settings on startup. Use 'Save & Restart Bot' to apply immediately.")
