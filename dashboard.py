"""
5m Poly Bot Dashboard v3 — Управление ботом, статистика, настройки, сохранение сигналов.
Единый источник данных: signals.json (Dashboard и Statistics синхронизированы).
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

# ===== DATA HELPERS — единый источник: signals.json =====
def load_saved_signals():
    """Load all saved signals from signals.json."""
    try:
        data = json.loads(SIGNALS_FILE.read_text())
        return data if isinstance(data, list) else []
    except:
        return []

def build_dashboard_state():
    """
    Строим состояние Dashboard из signals.json (единый источник данных).
    Возвращает dict с метриками, последними сигналами, скипами, трейдами.
    """
    signals = load_saved_signals()

    # Разделяем на вошедшие и пропущенные
    entered = [s for s in signals if s.get("entered")]
    skipped = [s for s in signals if not s.get("entered")]

    # Считаем invested и pnl из вошедших сигналов
    settings = load_settings()
    amount = settings.get("amount", 10)
    bank_start = float(settings.get("bank", 100))
    total_invested = sum(s.get("amount", amount) for s in entered)

    # PnL: берём из signal data, или рассчитываем из pm цены для старых сигналов
    # Формула: pnl = (amount / pm_price) - amount
    total_pnl = 0
    for s in entered:
        if s.get("pnl_expected") is not None:
            total_pnl += s["pnl_expected"]
        elif s.get("pm") and s["pm"] > 0:
            trade_amount = s.get("amount", amount)
            total_pnl += (trade_amount / s["pm"]) - trade_amount

    # Realized PnL — реальные результаты завершённых раундов
    realized_pnl = sum(s.get("realized_pnl", 0) for s in entered if s.get("realized_pnl") is not None)
    bank_current = bank_start + realized_pnl

    # Win/Loss статистика
    wins = [s for s in entered if s.get("won") == True]
    losses = [s for s in entered if s.get("won") == False]
    pending = [s for s in entered if "realized_pnl" not in s or s.get("realized_pnl") is None]

    # Skip reasons breakdown
    skip_reasons = {}
    for s in skipped:
        reason = s.get("reason", "other")
        # Нормализуем причины
        if "PM price <" in reason or "btc_low" in reason.lower():
            key = "btc_low"
        elif "PM price <" in reason or "eth_low" in reason.lower():
            key = "eth_low"
        elif "PM price >" in reason or ">0.99" in reason.lower():
            key = "high"
        elif "delta" in reason.lower():
            key = "delta"
        elif "confidence" in reason.lower():
            key = "conf"
        elif "atr" in reason.lower() or "ATR" in reason:
            key = "atr"
        else:
            key = "other"
        skip_reasons[key] = skip_reasons.get(key, 0) + 1

    # Последние сигналы (любые, не только вошедшие)
    last_signals = signals[-20:]
    last_entered = entered[-12:]
    last_skipped = skipped[-12:]

    # Последние цены BTC/ETH
    btc_price = None
    eth_price = None
    for s in reversed(signals):
        if s.get("coin") == "BTC" and s.get("price"):
            btc_price = s["price"]
        if s.get("coin") == "ETH" and s.get("price"):
            eth_price = s["price"]
        if btc_price and eth_price:
            break

    # Последние цены PM для BTC/ETH
    btc_pm = None
    eth_pm = None
    for s in reversed(signals):
        if s.get("coin") == "BTC" and s.get("pm") and not btc_pm:
            btc_pm = s
        if s.get("coin") == "ETH" and s.get("pm") and not eth_pm:
            eth_pm = s

    return {
        "total_signals": len(signals),
        "total_entered": len(entered),
        "total_skipped": len(skipped),
        "invested": total_invested,
        "pnl": total_pnl,
        "realized_pnl": realized_pnl,
        "bank_start": bank_start,
        "bank_current": bank_current,
        "wins": len(wins),
        "losses": len(losses),
        "pending": len(pending),
        "btc_price": btc_price,
        "eth_price": eth_price,
        "btc_pm": btc_pm,
        "eth_pm": eth_pm,
        "skip_reasons": skip_reasons,
        "last_signals": last_signals,
        "last_entered": last_entered,
        "last_skipped": last_skipped,
        "btc_signals": [s for s in signals if s.get("coin") == "BTC"],
        "eth_signals": [s for s in signals if s.get("coin") == "ETH"],
        "btc_entered": [s for s in entered if s.get("coin") == "BTC"],
        "eth_entered": [s for s in entered if s.get("coin") == "ETH"],
        "time": datetime.now().strftime('%H:%M:%S'),
    }

# Парсинг логов только для статуса (active/sleeping) — не для статистики
_P_STATE = {
    "active": re.compile(r'Active window.*?close\s+([\d:]+)'),
    "sleep":  re.compile(r'Sleeping\s+(\d+)s'),
    "snc":    re.compile(r'next close\s+([\d:]+)'),
    "closed": re.compile(r'Market closed'),
}

def parse_log_state(path):
    """Парсим логи только для определения текущего состояния бота."""
    if not path or not path.exists():
        return {'state': 'start', 'sleep': 0, 'nc': '--:--', 'round': 0, 'err': 0}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except:
        return {'state': 'start', 'sleep': 0, 'nc': '--:--', 'round': 0, 'err': 0}

    state = {'state': 'start', 'sleep': 0, 'nc': '--:--', 'round': 0, 'err': 0}
    for line in lines[-200:]:  # последние 200 строк достаточно для статуса
        if _P_STATE["active"].search(line):
            state['state'] = 'active'
            m = _P_STATE["active"].search(line)
            if m: state['nc'] = m.group(1)
        elif _P_STATE["sleep"].search(line):
            state['state'] = 'sleeping'
            m = _P_STATE["sleep"].search(line)
            if m: state['sleep'] = int(m.group(1))
            m3 = _P_STATE["snc"].search(line)
            if m3: state['nc'] = m3.group(1)
        elif _P_STATE["closed"].search(line):
            state['round'] += 1
        elif 'BINANCE ERROR' in line:
            state['err'] += 1
    return state

def load_settings():
    """Load bot settings."""
    try:
        return json.loads((BOT_DIR / "settings.json").read_text())
    except:
        return get_default_settings()

def get_default_settings():
    """Возвращает дефолтные настройки бота."""
    return {
        "bank": 100,
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

# Пресеты настроек для разных стратегий
PRESETS = {
    "default": {
        "name": "📋 По умолчанию",
        "desc": "Стандартные настройки — баланс риска и прибыли",
        "settings": {
            "bank": 100,
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
    },
    "conservative": {
        "name": "🛡️ Консервативный",
        "desc": "Меньше сделок, выше качество. Минимальный риск.",
        "settings": {
            "bank": 100,
            "mode": "dry-run",
            "amount": 5,
            "min_confidence": 0.55,
            "entry_min": 15,
            "entry_max": 45,
            "price_min_btc": 0.96,
            "price_min_eth": 0.94,
            "price_max": 0.99,
            "delta_skip": 0.001,
            "atr_multiplier": 2.0,
        }
    },
    "balanced": {
        "name": "⚖️ Сбалансированный",
        "desc": "Оптимальный баланс частоты и качества сделок.",
        "settings": {
            "bank": 100,
            "mode": "dry-run",
            "amount": 10,
            "min_confidence": 0.4,
            "entry_min": 10,
            "entry_max": 50,
            "price_min_btc": 0.95,
            "price_min_eth": 0.93,
            "price_max": 0.99,
            "delta_skip": 0.0007,
            "atr_multiplier": 1.8,
        }
    },
    "aggressive": {
        "name": "🔥 Агрессивный",
        "desc": "Больше сделок, выше риск. Для тестирования стратегии.",
        "settings": {
            "bank": 100,
            "mode": "dry-run",
            "amount": 20,
            "min_confidence": 0.2,
            "entry_min": 5,
            "entry_max": 55,
            "price_min_btc": 0.92,
            "price_min_eth": 0.90,
            "price_max": 0.99,
            "delta_skip": 0.0003,
            "atr_multiplier": 1.2,
        }
    },
    "high_amount": {
        "name": "💰 Крупные ставки",
        "desc": "Больший размер ставки при стандартных параметрах.",
        "settings": {
            "bank": 500,
            "mode": "dry-run",
            "amount": 50,
            "min_confidence": 0.4,
            "entry_min": 10,
            "entry_max": 50,
            "price_min_btc": 0.94,
            "price_min_eth": 0.92,
            "price_max": 0.99,
            "delta_skip": 0.0005,
            "atr_multiplier": 1.5,
        }
    },
}

def save_settings(settings):
    try:
        (BOT_DIR / "settings.json").write_text(json.dumps(settings, indent=2))
    except: pass

def load_custom_presets():
    """Загружает пользовательские пресеты из файла."""
    try:
        data = json.loads((BOT_DIR / "presets.json").read_text())
        return data if isinstance(data, dict) else {}
    except:
        return {}

def save_custom_presets(presets):
    """Сохраняет пользовательские пресеты."""
    try:
        (BOT_DIR / "presets.json").write_text(json.dumps(presets, indent=2))
    except: pass

# ===== LABELS для skip reasons =====
RL = {'btc_low': 'BTC<0.94', 'eth_low': 'ETH<0.92', 'high': '>0.99',
      'delta': 'δ<0.05%', 'conf': 'conf<30%', 'atr': 'ATR↑', 'other': '?'}

# ===== INIT =====
settings = load_settings()
bot_running = is_bot_running()
D = build_dashboard_state()  # Dashboard state из signals.json
L = parse_log_state(Path(os.environ.get("BOT_LOG_FILE", str(DEFAULT_LOG))))  # Log state только для статуса

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

    status_text = f"{'🟢' if bot_running else '🔴'}"
    bank = D['bank_current']
    bank_change = bank - D['bank_start']
    ctrl_cols[4].markdown(
        f"{status_text} `{settings.get('mode', 'dry-run')}` | "
        f"${settings.get('amount', 10)}/trade | "
        f"🏦 **${bank:.0f}** ({bank_change:+.2f}) | "
        f"🕐 `{D['time']}`"
    )

    ctrl_cols[5].caption(f"PID: {PID_FILE.read_text().strip() if PID_FILE.exists() else '—'}")

    st.markdown("---")

    # ---- STATUS BAR ----
    ico = {'sleeping': '💤', 'active': '⚡', 'start': '🚀'}.get(L['state'], '⚪')
    st.markdown(f"### {ico} {L['state'].upper()} — Next: {L['nc']} | Sleep: {L['sleep']}s")

    skip_rate = D['total_skipped'] / max(D['total_signals'], 1) * 100
    a1, a2, a3, a4, a5, a6 = st.columns(6)
    a1.metric("Signals", D['total_signals'])
    a2.metric("Rounds", L['round'])
    a3.metric("Entries", D['total_entered'])
    a4.metric("Binance", "❌ Err" if L['err'] else "✅ OK")
    a5.metric("Skip Rate", f"{skip_rate:.0f}%")
    a6.metric("Updated", D['time'])

    # ---- PNL + INVESTED ----
    st.markdown("---")
    b1, b2, b3 = st.columns(3)
    pnl_v = D['realized_pnl']
    b1.metric("💰 Realized PnL", f"${pnl_v:+.2f}",
               delta=f"{pnl_v:+.2f}",
               delta_color="normal" if pnl_v >= 0 else "inverse")
    b2.metric("📊 Invested", f"${D['invested']:.2f}")
    b3.metric("📈 Expected PnL", f"${D['pnl']:+.2f}")

    # ---- WIN/LOSS (only when resolved trades exist) ----
    if D['wins'] > 0 or D['losses'] > 0:
        w1, w2, w3 = st.columns(3)
        win_rate = D['wins'] / max(D['wins'] + D['losses'], 1) * 100
        w1.metric("✅ Wins", D['wins'])
        w2.metric("❌ Losses", D['losses'])
        w3.metric("🎯 Win Rate", f"{win_rate:.0f}%")

    # ---- PRICES ----
    st.markdown("---")
    p1, p2 = st.columns(2)
    p1.metric("BTC", f"${D['btc_price']:.0f}" if D['btc_price'] else "—")
    p2.metric("ETH", f"${D['eth_price']:.0f}" if D['eth_price'] else "—")

    # ---- PM PRICES + SKIP REASONS ----
    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Polymarket**")
        for sig, name, mn in [
            (D['btc_pm'], "BTC", settings.get("price_min_btc", 0.94)),
            (D['eth_pm'], "ETH", settings.get("price_min_eth", 0.92)),
        ]:
            if sig and sig.get('pm'):
                p = sig['pm']
                ok = mn <= p <= settings.get("price_max", 0.99)
                col = "#51cf66" if ok else "#ff6b6b"
                lbl = "✓" if ok else "✗"
                st.markdown(f'`{name}` <span style="color:{col};font-weight:bold">{p:.3f} {lbl}</span> '
                            f'δ{sig.get("delta", 0):.2f}% C{int(sig.get("confidence", 0)*100)}%',
                            unsafe_allow_html=True)
            else:
                st.markdown(f"`{name}` —")
    with c2:
        if D['skip_reasons']:
            st.markdown("**Why skipping**")
            tot = D['total_skipped']
            for rr, cnt in sorted(D['skip_reasons'].items(), key=lambda x: -x[1]):
                pct = cnt / max(tot, 1)
                st.progress(pct, text=f"{RL.get(rr, rr)}: {cnt} ({pct*100:.0f}%)")
        else:
            st.caption("No skips")

    # ---- TRADES + SKIP CHIPS ----
    if D['last_entered'] or D['last_skipped']:
        st.markdown("---")
        d1, d2 = st.columns(2)
        with d1:
            if D['last_entered']:
                st.markdown("**Last Entries**")
                trade_html = '<div style="display:flex;flex-wrap:wrap;gap:4px;">'
                for x in reversed(D['last_entered'][-8:]):
                    pnl = x.get("pnl_expected", 0)
                    sign = "+" if pnl >= 0 else ""
                    trade_html += f'<span class="trade-chip">{x.get("coin", "")} {sign}${pnl:.2f}</span>'
                trade_html += '</div>'
                st.markdown(trade_html, unsafe_allow_html=True)
        with d2:
            if D['last_skipped']:
                st.markdown("**Last Skips**")
                skip_html = '<div style="display:flex;flex-wrap:wrap;gap:4px;">'
                for x in reversed(D['last_skipped'][-12:]):
                    reason = x.get("reason", "?")[:20]
                    skip_html += f'<span class="skip-chip">{x.get("coin", "")} {reason}</span>'
                skip_html += '</div>'
                st.markdown(skip_html, unsafe_allow_html=True)

    # ---- SIGNALS TABLE ----
    if D['last_signals']:
        with st.expander(f"📋 Last Signals ({len(D['last_signals'])})", expanded=False):
            rows = []
            for x in reversed(D['last_signals']):
                rows.append({
                    "Coin": x.get('coin', ''),
                    "Side": x.get('side', ''),
                    "PM": f"{x.get('pm', 0):.3f}",
                    "Δ%": f"{x.get('delta', 0):.3f}",
                    "Conf%": f"{x.get('confidence', 0)*100:.0f}",
                    "Price": f"{x.get('price', 0):.0f}",
                    "Entered": "✅" if x.get("entered") else "❌",
                })
            st.dataframe(rows, use_container_width=True, hide_index=True, height=200)

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
    st.caption("Данные из signals.json — синхронизированы с Dashboard")

    if D['total_signals'] > 0:
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Total Signals", D['total_signals'])
        s2.metric("BTC Signals", len(D['btc_signals']))
        s3.metric("ETH Signals", len(D['eth_signals']))
        s4.metric("Entries", D['total_entered'])

        st.markdown("---")

        # Win rate by coin
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**BTC Stats**")
            if D['btc_entered']:
                st.metric("Entries", len(D['btc_entered']))
                avg_conf = sum(x.get("confidence", 0) for x in D['btc_entered']) / len(D['btc_entered'])
                st.metric("Avg Confidence", f"{avg_conf:.0%}")
                avg_delta = sum(x.get("delta", 0) for x in D['btc_entered']) / len(D['btc_entered'])
                st.metric("Avg Delta", f"{avg_delta:.4f}%")
                btc_pnl = sum(x.get("pnl_expected", 0) for x in D['btc_entered'])
                st.metric("Total PnL", f"${btc_pnl:+.2f}", delta=f"{btc_pnl:+.2f}")
            else:
                st.caption("No BTC entries yet")

        with col2:
            st.markdown("**ETH Stats**")
            if D['eth_entered']:
                st.metric("Entries", len(D['eth_entered']))
                avg_conf = sum(x.get("confidence", 0) for x in D['eth_entered']) / len(D['eth_entered'])
                st.metric("Avg Confidence", f"{avg_conf:.0%}")
                avg_delta = sum(x.get("delta", 0) for x in D['eth_entered']) / len(D['eth_entered'])
                st.metric("Avg Delta", f"{avg_delta:.4f}%")
                eth_pnl = sum(x.get("pnl_expected", 0) for x in D['eth_entered'])
                st.metric("Total PnL", f"${eth_pnl:+.2f}", delta=f"{eth_pnl:+.2f}")
            else:
                st.caption("No ETH entries yet")

        st.markdown("---")

        # Skip reasons breakdown
        st.markdown("**Skip Reasons Breakdown**")
        if D['skip_reasons']:
            tot = D['total_skipped']
            for rr, cnt in sorted(D['skip_reasons'].items(), key=lambda x: -x[1]):
                pct = cnt / max(tot, 1)
                st.progress(pct, text=f"{RL.get(rr, rr)}: {cnt} ({pct*100:.0f}%)")
        else:
            st.caption("No skip data")

        # Summary
        st.markdown("---")
        st.markdown("**Summary**")
        su1, su2, su3, su4 = st.columns(4)
        su1.metric("🏦 Банк", f"${D['bank_current']:.2f}", delta=f"{D['bank_current']-D['bank_start']:+.2f}")
        su2.metric("💰 Realized PnL", f"${D['realized_pnl']:+.2f}")
        su3.metric("📊 Invested", f"${D['invested']:.2f}")
        su4.metric("📈 Expected PnL", f"${D['pnl']:+.2f}")

        # Win/Loss breakdown
        st.markdown("---")
        st.markdown("**Win/Loss Breakdown**")
        wl1, wl2, wl3, wl4 = st.columns(4)
        wl1.metric("✅ Wins", D['wins'])
        wl2.metric("❌ Losses", D['losses'])
        total_resolved = D['wins'] + D['losses']
        win_rate = D['wins'] / max(total_resolved, 1) * 100
        wl3.metric("🎯 Win Rate", f"{win_rate:.0f}%" if total_resolved > 0 else "—")
        wl4.metric("⏳ Pending Results", D['pending'])

        # Per-coin realized PnL
        st.markdown("---")
        st.markdown("**Realized PnL by Coin**")
        rp1, rp2 = st.columns(2)
        with rp1:
            st.markdown("**BTC**")
            btc_realized = sum(x.get("realized_pnl", 0) for x in D['btc_entered'] if x.get("realized_pnl") is not None)
            btc_wins = len([x for x in D['btc_entered'] if x.get("won") == True])
            btc_losses = len([x for x in D['btc_entered'] if x.get("won") == False])
            st.metric("Realized PnL", f"${btc_realized:+.2f}")
            st.caption(f"Wins: {btc_wins} | Losses: {btc_losses}")
        with rp2:
            st.markdown("**ETH**")
            eth_realized = sum(x.get("realized_pnl", 0) for x in D['eth_entered'] if x.get("realized_pnl") is not None)
            eth_wins = len([x for x in D['eth_entered'] if x.get("won") == True])
            eth_losses = len([x for x in D['eth_entered'] if x.get("won") == False])
            st.metric("Realized PnL", f"${eth_realized:+.2f}")
            st.caption(f"Wins: {eth_wins} | Losses: {eth_losses}")

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

    # ===== PRESETS BAR =====
    st.markdown("**📦 Быстрые пресеты**")
    st.caption("Выберите готовую стратегию или настройте вручную")

    custom_presets = load_custom_presets()
    all_presets = {**PRESETS, **custom_presets}

    # Колонки для пресетов
    preset_cols = st.columns(5)
    selected_preset = None

    for i, (key, preset) in enumerate(all_presets.items()):
        with preset_cols[i % 5]:
            label = preset["name"]
            is_current = all(v == settings.get(k) for k, v in preset["settings"].items())
            btn_type = "primary" if is_current else "secondary"
            if st.button(label, use_container_width=True, type=btn_type, key=f"preset_{key}"):
                selected_preset = preset["settings"].copy()

    if selected_preset:
        settings.update(selected_preset)
        st.success(f"✅ Применён пресет: {all_presets[[k for k, v in all_presets.items() if v['settings'] == selected_preset][0]]['name']}")
        st.rerun()

    # Описание выбранного пресета
    st.markdown("---")
    st.markdown("**Как работают пресеты:**")
    desc_cols = st.columns(3)
    with desc_cols[0]:
        st.info("🛡️ **Консервативный**\n- Меньше сделок\n- Выше уверенность (55%)\n- Меньше размер ставки\n- Для минимального риска")
    with desc_cols[1]:
        st.info("⚖️ **Сбалансированный**\n- Среднее кол-во сделок\n- Уверенность 40%\n- Стандартный размер ставки\n- Оптимальный баланс")
    with desc_cols[2]:
        st.info("🔥 **Агрессивный**\n- Больше сделок\n- Ниже уверенность (20%)\n- Больше размер ставки\n- Для тестирования")

    st.markdown("---")

    # ===== MANUAL SETTINGS =====
    new_settings = settings.copy()

    s1, s2 = st.columns(2)
    with s1:
        st.markdown("**🎮 Run Configuration**")
        new_settings["bank"] = st.number_input(
            "🏦 Начальный банк (USDC)",
            min_value=10.0, max_value=100000.0,
            value=float(settings.get("bank", 100)),
            step=10.0,
            help="Начальный капитал для ставок. Баланс обновляется по результатам раундов."
        )
        new_settings["mode"] = st.selectbox(
            "Режим работы",
            ["dry-run", "paper", "live"],
            index=["dry-run", "paper", "live"].index(settings.get("mode", "dry-run")),
            help="dry-run: реальные данные без сделок | paper: симуляция | live: реальные деньги"
        )
        new_settings["amount"] = st.number_input(
            "Размер ставки (USDC)",
            min_value=1.0, max_value=1000.0,
            value=float(settings.get("amount", 10)),
            step=1.0,
            help="Сколько USDC вкладывать в каждую сделку"
        )

    with s2:
        st.markdown("**⏱️ Окно входа**")
        new_settings["entry_min"] = st.number_input(
            "Мин. секунд до закрытия",
            min_value=1, max_value=120,
            value=int(settings.get("entry_min", 10)),
            step=1,
            help="Начинать искать вход не раньше чем за X сек до закрытия"
        )
        new_settings["entry_max"] = st.number_input(
            "Макс. секунд до закрытия",
            min_value=5, max_value=300,
            value=int(settings.get("entry_max", 50)),
            step=5,
            help="Перестать искать вход за X сек до закрытия"
        )

    st.markdown("---")
    s3, s4 = st.columns(2)
    with s3:
        st.markdown("**💲 Ценовые пороги**")
        new_settings["price_min_btc"] = st.number_input(
            "BTC мин. цена Polymarket",
            min_value=0.50, max_value=1.0,
            value=float(settings.get("price_min_btc", 0.94)),
            step=0.01, format="%.2f",
            help="Не входить в BTC если цена < этого значения"
        )
        new_settings["price_min_eth"] = st.number_input(
            "ETH мин. цена Polymarket",
            min_value=0.50, max_value=1.0,
            value=float(settings.get("price_min_eth", 0.92)),
            step=0.01, format="%.2f",
            help="Не входить в ETH если цена < этого значения"
        )
        new_settings["price_max"] = st.number_input(
            "Макс. цена (оба)",
            min_value=0.90, max_value=1.0,
            value=float(settings.get("price_max", 0.99)),
            step=0.01, format="%.2f",
            help="Не входить если цена > этого (мало профита)"
        )

    with s4:
        st.markdown("**📊 Пороги сигналов**")
        new_settings["min_confidence"] = st.slider(
            "Мин. уверенность",
            min_value=0.0, max_value=1.0,
            value=float(settings.get("min_confidence", 0.3)),
            step=0.05,
            help="Минимальная уверенность сигнала для входа (0-100%)"
        )
        new_settings["delta_skip"] = st.number_input(
            "Мин. дельта (%)",
            min_value=0.0001, max_value=0.01,
            value=float(settings.get("delta_skip", 0.0005)),
            step=0.0001, format="%.4f",
            help="Пропускать сигнал если дельта цены < этого значения"
        )
        new_settings["atr_multiplier"] = st.number_input(
            "ATR множитель",
            min_value=0.5, max_value=5.0,
            value=float(settings.get("atr_multiplier", 1.5)),
            step=0.1,
            help="Пропускать если волатильность > ATR × множитель"
        )

    # ===== EXPLANATION =====
    st.markdown("---")
    with st.expander("❓ Что означает каждый параметр?", expanded=False):
        st.markdown("""
        ### 🎮 Run Configuration
        - **Режим**: dry-run (безопасно, реальные данные), paper (симуляция), live (реальные деньги!)
        - **Размер ставки**: сколько USDC вкладывать в каждую сделку

        ### ⏱️ Окно входа
        - **Мин/Макс секунд**: бот ищет вход только в этом окне перед закрытием 5-мин свечи
        - Пример: 10-50 сек = начинает искать за 50 сек, заканчивает за 10 сек

        ### 💲 Ценовые пороги
        - **BTC/ETH мин. цена**: не входить если цена Polymarket слишком низкая (мало профита)
        - **Макс. цена**: не входить если цена слишком высокая (>0.99 = минимум профита)

        ### 📊 Пороги сигналов
        - **Мин. уверенность**: насколько бот должен быть уверен в сигнале (0.3 = 30%)
        - **Мин. дельта**: минимальное изменение цены для входа (0.0005 = 0.05%)
        - **ATR множитель**: фильтр волатильности (1.5 = пропускать если волатильность > 1.5× нормы)
        """)

    # ===== BUTTONS =====
    st.markdown("---")
    btn_cols = st.columns([1, 1, 1, 1])

    with btn_cols[0]:
        if st.button("💾 Сохранить", type="primary", use_container_width=True):
            save_settings(new_settings)
            st.success("✅ Настройки сохранены!")
            st.rerun()

    with btn_cols[1]:
        if st.button("💾 Сохранить и рестарт", use_container_width=True):
            save_settings(new_settings)
            restart_bot(new_settings["mode"], new_settings["amount"])
            st.success("✅ Сохранено и перезапущено!")
            st.rerun()

    with btn_cols[2]:
        if st.button("🔄 Сбросить к дефолтным", use_container_width=True):
            defaults = get_default_settings()
            save_settings(defaults)
            st.success("✅ Сброшено к настройкам по умолчанию!")
            st.rerun()

    with btn_cols[3]:
        if st.button("🏦 Сбросить банк", use_container_width=True):
            s = load_settings()
            s["bank"] = float(settings.get("bank", 100))
            save_settings(s)
            st.success(f"✅ Банк сброшен до ${s['bank']:.0f}!")
            st.rerun()

    # Save custom preset
    st.markdown("---")
    preset_cols = st.columns([2, 1])
    with preset_cols[0]:
        preset_name = st.text_input("Сохранить текущие настройки как пресет:", placeholder="Название пресета...", key="new_preset_name")
    with preset_cols[1]:
        if preset_name and st.button("💾 Сохранить пресет", use_container_width=True, type="secondary"):
            custom = load_custom_presets()
            preset_key = preset_name.lower().replace(" ", "_")
            custom[preset_key] = {
                "name": preset_name,
                "desc": "Пользовательский пресет",
                "settings": new_settings
            }
            save_custom_presets(custom)
            st.success(f"✅ Пресет '{preset_name}' сохранён!")
            st.rerun()

    st.caption("Настройки сохраняются в `settings.json`. Бот читает их при запуске. Используйте 'Сохранить и рестарт' для немедленного применения.")
