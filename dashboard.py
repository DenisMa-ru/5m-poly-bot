"""
5m Poly Bot Dashboard v3 — Управление ботом, статистика, настройки, сохранение сигналов.
Единый источник данных: signals.json (Dashboard и Statistics синхронизированы).
Запуск: streamlit run dashboard.py --server.port 3001 --server.address 0.0.0.0 --server.headless true
"""
import streamlit as st
import re, os, json
import requests
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

# ===== CONFIG =====
DEFAULT_LOG = Path("/root/5m-poly-bot/bot.log")
CONTROL_FILE = Path("/root/5m-poly-bot/control.json")
SIGNALS_FILE = Path("/root/5m-poly-bot/signals.json")
BOT_DIR = Path("/root/5m-poly-bot")
BOT_SCRIPT = "crypto_bot.py"
PID_FILE = Path("/root/5m-poly-bot/bot.pid")
CLOB_API = "https://clob.polymarket.com"
POLYMARKET_DATA_API = "https://data-api.polymarket.com"

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
    .safe-box {
        padding: 0.85rem 1rem;
        border: 1px solid #30363d;
        border-radius: 10px;
        background: #0d1117;
        margin-bottom: 0.75rem;
    }
    .safe-box strong { color: #f0f6fc; }
    .summary-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
        margin: 0.35rem 0 0.5rem;
    }
    .summary-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 10px;
        border: 1px solid #30363d;
        border-radius: 999px;
        background: #11161d;
        color: #c9d1d9;
        font-size: 0.9rem;
        line-height: 1.2;
        white-space: nowrap;
    }
    .summary-chip strong { color: #f0f6fc; }
</style>
""", unsafe_allow_html=True)

# ===== BOT CONTROL HELPERS =====
def atomic_write_text(path: Path, content: str):
    """Atomically write text to a file to reduce corruption risk."""
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def write_control(cmd, mode=None, amount=None, settings=None):
    """Write command to control file for the bot to read."""
    data = {"cmd": cmd, "timestamp": datetime.now().isoformat()}
    if mode: data["mode"] = mode
    if amount: data["amount"] = amount
    if settings: data["settings"] = settings
    try:
        atomic_write_text(CONTROL_FILE, json.dumps(data))
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

# ===== DATA HELPERS — единый источник: signals.json =====
def load_saved_signals():
    """Load all saved signals from signals.json."""
    try:
        data = json.loads(SIGNALS_FILE.read_text())
        return data if isinstance(data, list) else []
    except:
        return []


def _safe_float(value):
    try:
        if isinstance(value, bool):
            return None
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _normalize_usdc_amount(value):
    """Normalize raw USDC values when APIs return 6-decimal base units."""
    if value is None:
        return None
    if abs(value) >= 100000:
        return value / 1_000_000
    return value


def _looks_like_valid_portfolio(value, cash, positions_value):
    if value is None:
        return False
    if cash is None and positions_value is None:
        return value >= 0
    expected_floor = max(cash or 0, 0)
    expected_total = (cash or 0) + (positions_value or 0)
    tolerance = 0.05
    return value + tolerance >= expected_floor and value <= expected_total + tolerance


def get_collateral_balance_allowance(private_key: str, proxy_wallet: str) -> tuple[float | None, float | None]:
    """Return available collateral balance and allowance from Polymarket, if available."""
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

        balance = _normalize_usdc_amount(_safe_float(raw.get("balance")))
        allowance = _normalize_usdc_amount(_safe_float(raw.get("allowance")))

        if allowance is None:
            nested_allowances = raw.get("allowances") or raw.get("allowanceData") or {}
            if isinstance(nested_allowances, dict):
                values = [_normalize_usdc_amount(_safe_float(v)) for v in nested_allowances.values()]
                values = [v for v in values if v is not None]
                if values:
                    allowance = max(values)

        return balance, allowance
    except Exception:
        return None, None


def fetch_polymarket_account_state() -> dict:
    """Fetch real Polymarket cash and portfolio values for dashboard display."""
    private_key = os.getenv("POLY_PRIVATE_KEY", "")
    proxy_wallet = os.getenv("POLY_PROXY_WALLET", "")
    user_address = proxy_wallet or os.getenv("POLY_WALLET", "")

    result = {
        "cash": None,
        "spendable": None,
        "allowance": None,
        "portfolio": None,
        "redeemable": None,
        "positions_value": None,
        "open_positions": None,
        "wallet": user_address or None,
        "source_error": None,
    }

    if private_key:
        cash, allowance = get_collateral_balance_allowance(private_key, proxy_wallet)
        result["cash"] = cash
        result["allowance"] = allowance
        if cash is not None and allowance is not None:
            result["spendable"] = min(cash, allowance)
        elif cash is not None:
            result["spendable"] = cash

    if not user_address:
        if not private_key:
            result["source_error"] = "Missing POLY_PRIVATE_KEY/POLY_PROXY_WALLET"
        else:
            result["source_error"] = "Missing POLY_PROXY_WALLET"
        return result

    portfolio_from_value = None

    try:
        value_resp = requests.get(
            f"{POLYMARKET_DATA_API}/value",
            params={"user": user_address},
            timeout=5,
        )
        value_resp.raise_for_status()
        payload = value_resp.json()
        if isinstance(payload, dict):
            for key in ("value", "currentValue", "portfolioValue", "totalValue"):
                portfolio = _normalize_usdc_amount(_safe_float(payload.get(key)))
                if portfolio is not None:
                    portfolio_from_value = portfolio
                    break
    except Exception as exc:
        result["source_error"] = str(exc)

    try:
        positions_resp = requests.get(
            f"{POLYMARKET_DATA_API}/positions",
            params={"user": user_address, "sizeThreshold": 0},
            timeout=5,
        )
        positions_resp.raise_for_status()
        payload = positions_resp.json()
        positions = payload if isinstance(payload, list) else payload.get("positions", []) if isinstance(payload, dict) else []

        if isinstance(positions, list):
            redeemable = 0.0
            positions_value = 0.0
            open_positions = 0
            has_redeemable = False
            has_positions_value = False

            for pos in positions:
                if not isinstance(pos, dict):
                    continue

                current_value = _normalize_usdc_amount(_safe_float(pos.get("currentValue")))
                if current_value is None:
                    current_value = _normalize_usdc_amount(_safe_float(pos.get("value")))
                if current_value is not None:
                    positions_value += current_value
                    if current_value > 0:
                        open_positions += 1
                    has_positions_value = True

                redeem_value = _normalize_usdc_amount(_safe_float(pos.get("redeemableValue")))
                if redeem_value is None:
                    redeem_value = _normalize_usdc_amount(_safe_float(pos.get("redeemedValue")))
                if redeem_value is None and pos.get("redeemable") is True:
                    redeem_value = current_value
                if redeem_value is None and pos.get("redeemable") is True:
                    redeem_value = _normalize_usdc_amount(_safe_float(pos.get("size")))
                if redeem_value is not None:
                    redeemable += redeem_value
                    has_redeemable = True

            if has_positions_value:
                result["positions_value"] = positions_value
            result["open_positions"] = open_positions
            if has_redeemable:
                result["redeemable"] = redeemable
    except Exception as exc:
        if result["source_error"] is None:
            result["source_error"] = str(exc)

    positions_value = result["positions_value"]
    fallback_portfolio = None
    if result["cash"] is not None or positions_value is not None:
        fallback_portfolio = (result["cash"] or 0) + (positions_value or 0)

    if _looks_like_valid_portfolio(portfolio_from_value, result["cash"], positions_value):
        result["portfolio"] = portfolio_from_value
    else:
        result["portfolio"] = fallback_portfolio

    if result["redeemable"] is None:
        result["redeemable"] = 0.0 if positions_value is not None else None

    return result

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

    account_state = fetch_polymarket_account_state()

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
        "account": account_state,
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
        "mode": "live",
        "amount": 5,
        "enabled_coins": ["BTC"],
        "daily_loss_limit": 10.0,
        "daily_loss_limit_pct": 0.20,
        "dynamic_sizing": True,
        "dynamic_min_amount": 5.0,
        "dynamic_max_amount": 15.0,
        "dynamic_base_risk_pct": 0.05,
        "dynamic_step_bank_gain_pct": 0.70,
        "dynamic_step_risk_pct": 0.01,
        "dynamic_max_risk_pct": 0.08,
        "min_confidence": 0.0,
        "entry_min": 15,
        "entry_max": 20,
        "price_min_btc": 0.55,
        "price_min_eth": 0.70,
        "price_max": 0.70,
        "delta_skip": 0.0,
        "atr_multiplier": 1.5,
    }

def save_settings(settings):
    try:
        atomic_write_text(BOT_DIR / "settings.json", json.dumps(settings, indent=2))
    except: pass

# ===== LABELS для skip reasons =====
RL = {'btc_low': 'BTC below min', 'eth_low': 'ETH below min', 'high': 'Above max',
      'delta': 'Delta too low', 'conf': 'Confidence too low', 'atr': 'ATR filter', 'other': '?'}

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
    st.markdown("### 🎮 Bot Status")
    ctrl_cols = st.columns([1, 4, 1])

    with ctrl_cols[0]:
        status_label = "🟢 Running" if bot_running else "🔴 Stopped"
        st.button(status_label, use_container_width=True, disabled=True)

    with ctrl_cols[1]:
        st.markdown(
            "<div class='safe-box'><strong>Managed by systemd.</strong> "
            "The dashboard is read-only for runtime control to avoid duplicate bot processes and broken signal history.</div>",
            unsafe_allow_html=True,
        )

    with ctrl_cols[2]:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()
        st.caption(f"Updated: {D['time']}")

    account = D['account']
    mode = settings.get('mode', 'dry-run')
    is_live_mode = mode == 'live'
    cash = account.get('cash')
    portfolio = account.get('portfolio')
    spendable = account.get('spendable')
    redeemable = account.get('redeemable')
    bank = D['bank_current']
    bank_change = bank - D['bank_start']
    net_result = portfolio - D['bank_start'] if portfolio is not None else None
    roi_pct = (net_result / D['bank_start'] * 100) if net_result is not None and D['bank_start'] else None
    summary_items = [
        ("Mode", mode),
        ("Trade", f"${settings.get('amount', 10):.0f}"),
        ("Coins", ', '.join(settings.get('enabled_coins', ['BTC', 'ETH']))),
        ("PID", PID_FILE.read_text().strip() if PID_FILE.exists() else '—'),
    ]
    summary_html = '<div class="summary-bar">'
    for label, value in summary_items:
        summary_html += f'<span class="summary-chip"><strong>{label}</strong> {value}</span>'
    summary_html += '</div>'
    st.markdown(summary_html, unsafe_allow_html=True)
    if account.get('source_error'):
        st.caption(f"Polymarket sync note: {account['source_error']}")

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

    # ---- WALLET ----
    st.markdown("---")
    pnl_v = D['realized_pnl']
    st.markdown("#### Wallet")
    w1, w2, w3, w4 = st.columns(4)
    w1.metric(
        "💵 Polymarket Cash" if is_live_mode else "💵 Polymarket Cash (ref)",
        f"${cash:.2f}" if cash is not None else "—"
    )
    w2.metric(
        "🧾 Polymarket Portfolio" if is_live_mode else "🧾 Polymarket Portfolio (ref)",
        f"${portfolio:.2f}" if portfolio is not None else "—"
    )
    w3.metric("💸 Spendable", f"${spendable:.2f}" if spendable is not None else "—")
    w4.metric("🎁 Redeemable", f"${redeemable:.2f}" if redeemable is not None else "—")

    # ---- STRATEGY ----
    st.markdown("#### Strategy")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("🏁 Start Bank", f"${D['bank_start']:.2f}")
    s2.metric(
        "🤖 Strategy Bank",
        f"${bank:.2f}",
        delta=f"{bank_change:+.2f}",
        delta_color="normal" if bank_change >= 0 else "inverse"
    )
    s3.metric("💰 Realized PnL", f"${pnl_v:+.2f}",
              delta=f"{pnl_v:+.2f}",
              delta_color="normal" if pnl_v >= 0 else "inverse")
    s4.metric(
        "📈 Portfolio vs Start" if is_live_mode else "📈 Portfolio vs Start (ref)",
        f"${net_result:+.2f}" if net_result is not None else "—",
        delta=f"{roi_pct:+.2f}%" if roi_pct is not None else None,
        delta_color="normal" if net_result is None or net_result >= 0 else "inverse",
    )
    st.caption(
        "Live mode: Polymarket Cash/Portfolio are real wallet values; "
        "Strategy Bank and Realized PnL come from resolved bot signals. "
        "Portfolio vs Start = current Polymarket portfolio minus the starting bank from Settings."
        if is_live_mode else
        "Paper / dry-run mode: Strategy Bank and Realized PnL are simulated from bot signals. "
        "Polymarket Cash/Portfolio are shown only as reference and are not changed by simulated trades."
    )

    # ---- WIN/LOSS (only when resolved trades exist) ----
    if D['wins'] > 0 or D['losses'] > 0:
        st.markdown("#### Strategy Quality")
        q1, q2, q3 = st.columns(3)
        win_rate = D['wins'] / max(D['wins'] + D['losses'], 1) * 100
        q1.metric("✅ Wins", D['wins'])
        q2.metric("❌ Losses", D['losses'])
        q3.metric("🎯 Win Rate", f"{win_rate:.0f}%")

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

        st.caption("History reset is disabled in the dashboard to protect live runtime data.")
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
        su1.metric("💵 Cash", f"${D['account']['cash']:.2f}" if D['account']['cash'] is not None else "—")
        su2.metric("🧾 Portfolio", f"${D['account']['portfolio']:.2f}" if D['account']['portfolio'] is not None else "—")
        su3.metric("🏦 Bot Bank", f"${D['bank_current']:.2f}", delta=f"{D['bank_current']-D['bank_start']:+.2f}")
        su4.metric("💰 Realized PnL", f"${D['realized_pnl']:+.2f}")

        su5, su6, su7, su8 = st.columns(4)
        su5.metric("📊 Invested", f"${D['invested']:.2f}")
        su6.metric("📈 Expected PnL", f"${D['pnl']:+.2f}")
        su7.metric("🔓 Spendable", f"${D['account']['spendable']:.2f}" if D['account']['spendable'] is not None else "—")
        su8.metric("🎟 Redeemable", f"${D['account']['redeemable']:.2f}" if D['account']['redeemable'] is not None else "—")

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

        st.caption("Statistics reset is disabled in the dashboard to protect live runtime data.")
    else:
        st.info("No statistics yet. Statistics are built from saved signals.")

# ==========================================
# TAB 4: SETTINGS
# ==========================================
with tab_settings:
    st.markdown("### ⚙️ Settings")
    st.markdown(
        "<div class='safe-box'><strong>Safe editing mode.</strong> "
        "Only the active strategy parameters are shown here. Current tested values are also the dashboard defaults.</div>",
        unsafe_allow_html=True,
    )

    settings_password = os.getenv("DASHBOARD_SETTINGS_PASSWORD", "")
    settings_unlocked = not settings_password

    # ===== MANUAL SETTINGS =====
    new_settings = settings.copy()

    s1, s2 = st.columns(2)
    with s1:
        new_settings["bank"] = st.number_input("Банк (USDC)", min_value=10.0, max_value=100000.0, value=float(settings.get("bank", 100)), step=10.0)
        new_settings["mode"] = st.selectbox("Режим", ["dry-run", "paper", "live"], index=["dry-run", "paper", "live"].index(settings.get("mode", "dry-run")))
        new_settings["enabled_coins"] = st.multiselect("Активные монеты", ["BTC", "ETH"], default=settings.get("enabled_coins", ["BTC", "ETH"]))
        new_settings["amount"] = st.number_input("Ставка (USDC)", min_value=1.0, max_value=1000.0, value=float(settings.get("amount", 10)), step=1.0)
        new_settings["daily_loss_limit"] = st.number_input("Дневной стоп-лосс (USDC)", min_value=1.0, max_value=1000.0, value=float(settings.get("daily_loss_limit", 15.0)), step=1.0)
        new_settings["daily_loss_limit_pct"] = st.slider("Дневной стоп-лосс (% от банка)", min_value=0.0, max_value=0.50, value=float(settings.get("daily_loss_limit_pct", 0.0)), step=0.05, format="%.2f")
        new_settings["dynamic_sizing"] = st.checkbox("Динамический размер ставки", value=bool(settings.get("dynamic_sizing", True)))
        new_settings["entry_min"] = st.number_input("Вход мин (сек)", min_value=1, max_value=120, value=int(settings.get("entry_min", 10)), step=1)
        new_settings["entry_max"] = st.number_input("Вход макс (сек)", min_value=5, max_value=300, value=int(settings.get("entry_max", 30)), step=5)

    with s2:
        new_settings["price_min_btc"] = st.number_input("BTC мин цена", min_value=0.50, max_value=1.0, value=float(settings.get("price_min_btc", 0.55)), step=0.01, format="%.2f")
        new_settings["price_min_eth"] = st.number_input("ETH мин цена", min_value=0.50, max_value=1.0, value=float(settings.get("price_min_eth", 0.70)), step=0.01, format="%.2f")
        new_settings["price_max"] = st.number_input("Макс цена", min_value=0.50, max_value=1.0, value=float(settings.get("price_max", 0.70)), step=0.01, format="%.2f")
        new_settings["min_confidence"] = st.slider("Мин уверенность", min_value=0.0, max_value=1.0, value=float(settings.get("min_confidence", 0.0)), step=0.05)
        new_settings["delta_skip"] = st.number_input("Мин дельта", min_value=0.0, max_value=0.01, value=float(settings.get("delta_skip", 0.0)), step=0.0001, format="%.4f")
        new_settings["atr_multiplier"] = st.number_input("ATR множитель", min_value=0.5, max_value=5.0, value=float(settings.get("atr_multiplier", 1.5)), step=0.1)
        new_settings["dynamic_min_amount"] = st.number_input("Мин ставка (USDC)", min_value=1.0, max_value=1000.0, value=float(settings.get("dynamic_min_amount", 5.0)), step=1.0)
        new_settings["dynamic_max_amount"] = st.number_input("Макс ставка (USDC)", min_value=1.0, max_value=1000.0, value=float(settings.get("dynamic_max_amount", 15.0)), step=1.0)
        new_settings["dynamic_base_risk_pct"] = st.slider("Базовый риск", min_value=0.01, max_value=0.10, value=float(settings.get("dynamic_base_risk_pct", 0.05)), step=0.01, format="%.2f")
        new_settings["dynamic_step_bank_gain_pct"] = st.slider("Шаг роста банка", min_value=0.10, max_value=2.0, value=float(settings.get("dynamic_step_bank_gain_pct", 0.70)), step=0.05, format="%.2f")
        new_settings["dynamic_step_risk_pct"] = st.slider("Прирост риска за шаг", min_value=0.0, max_value=0.05, value=float(settings.get("dynamic_step_risk_pct", 0.01)), step=0.01, format="%.2f")
        new_settings["dynamic_max_risk_pct"] = st.slider("Макс риск", min_value=0.01, max_value=0.15, value=float(settings.get("dynamic_max_risk_pct", 0.08)), step=0.01, format="%.2f")

    # ===== BUTTONS =====
    st.markdown("---")
    if settings_password:
        st.markdown("**Защита настроек**")
        entered_password = st.text_input(
            "Пароль для сохранения или сброса настроек",
            type="password",
            key="settings_password_input",
        )
        settings_unlocked = entered_password == settings_password
        if entered_password and not settings_unlocked:
            st.error("Неверный пароль")
        elif settings_unlocked:
            st.success("Доступ к настройкам открыт")
        else:
            st.info("Введите пароль и затем нажмите нужную кнопку ниже")

    btn_cols = st.columns([1, 1])

    with btn_cols[0]:
        if st.button("💾 Сохранить", type="primary", use_container_width=True):
            if not settings_unlocked:
                st.error("Введите правильный пароль для сохранения настроек")
            else:
                save_settings(new_settings)
                st.success("✅ Настройки сохранены. Применение новых значений выполняйте через systemd restart.")
                st.rerun()

    with btn_cols[1]:
        if st.button("🔄 Сбросить к дефолтным", use_container_width=True):
            if not settings_unlocked:
                st.error("Введите правильный пароль для сброса настроек")
            else:
                defaults = get_default_settings()
                save_settings(defaults)
                st.success("✅ Восстановлены безопасные дефолтные настройки текущей стратегии.")
                st.rerun()

    st.caption("Настройки сохраняются в `settings.json` атомарно. Для применения используйте перезапуск сервиса через systemd, а не из dashboard.")
    if not settings_password:
        st.caption("Защита настроек не включена. Чтобы включить пароль, задайте переменную `DASHBOARD_SETTINGS_PASSWORD` в systemd service.")
