"""
5m Poly Bot Dashboard v3 — Управление ботом, статистика, настройки, сохранение сигналов.
Единый источник данных: signals.json (Dashboard и Statistics синхронизированы).
Запуск: streamlit run dashboard.py --server.port 3001 --server.address 0.0.0.0 --server.headless true
"""
import streamlit as st
import re, os, json, html
import subprocess
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pathlib import Path
import uuid
import time
from collections import deque

# ===== CONFIG =====
BOT_DIR = Path("/root/5m-poly-bot")
load_dotenv(BOT_DIR / ".env")
load_dotenv()

DEFAULT_LOG = Path("/root/5m-poly-bot/bot.log")
LIVE_LOG = Path("/root/5m-poly-bot/bot-live.log")
SIGNALS_FILE = Path("/root/5m-poly-bot/signals.json")
WINDOW_SAMPLES_FILE = Path("/root/5m-poly-bot/window_samples.json")
WINDOW_SAMPLES_JSONL_FILE = Path("/root/5m-poly-bot/window_samples.jsonl")
CORE_EV_RULES_FILE = Path("/root/5m-poly-bot/core_ev_rules.json")
SETTINGS_FILE = Path("/root/5m-poly-bot/settings.json")
SESSION_STATE_FILE = Path("/root/5m-poly-bot/session_state.json")
STATS_STATE_FILE = Path("/root/5m-poly-bot/stats_state.json")
BOT_SCRIPT = "crypto_bot.py"
PID_FILE = Path("/root/5m-poly-bot/bot.pid")
CLOB_API = "https://clob.polymarket.com"
POLYMARKET_DATA_API = "https://data-api.polymarket.com"
PUSD_TOKEN = os.getenv("POLY_PUSD_TOKEN", "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
BOT_SERVICE_CANDIDATES = [
    {"name": "poly-bot-live.service", "default_mode": "live", "default_log": LIVE_LOG},
    {"name": "poly-bot-test.service", "default_mode": "dry-run", "default_log": DEFAULT_LOG},
    {"name": "poly-bot.service", "default_mode": "dry-run", "default_log": DEFAULT_LOG},
]

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
    .log-viewer {
        max-height: 420px;
        overflow-y: auto;
        border: 1px solid #30363d;
        border-radius: 10px;
        background: #0b0f14;
        padding: 8px 0;
    }
    .log-line {
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
        font-size: 0.84rem;
        line-height: 1.45;
        color: #c9d1d9;
        padding: 4px 10px 4px 12px;
        border-left: 3px solid transparent;
        white-space: pre-wrap;
        word-break: break-word;
    }
    .log-line + .log-line { border-top: 1px solid rgba(48, 54, 61, 0.28); }
    .log-line.log-entering { border-left-color: #58a6ff; background: rgba(56, 139, 253, 0.08); }
    .log-line.log-core-allow { border-left-color: #3fb950; background: rgba(63, 185, 80, 0.08); }
    .log-line.log-core-deny { border-left-color: #f85149; background: rgba(248, 81, 73, 0.08); }
    .log-line.log-win { border-left-color: #2ea043; background: rgba(46, 160, 67, 0.10); }
    .log-line.log-loss { border-left-color: #ff7b72; background: rgba(255, 123, 114, 0.10); }
    .log-line.log-error { border-left-color: #db6d28; background: rgba(219, 109, 40, 0.10); }
</style>
""", unsafe_allow_html=True)

# ===== BOT CONTROL HELPERS =====
def atomic_write_text(path: Path, content: str):
    """Atomically write text to a file to reduce corruption risk."""
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


class FileLock:
    def __init__(self, path: Path, timeout: float = 5.0):
        self.path = path.with_suffix(path.suffix + ".lock")
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        started = time.time()
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return self
            except FileExistsError:
                if time.time() - started >= self.timeout:
                    raise TimeoutError(f"Timed out acquiring lock for {self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


def _load_json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return default


def _save_json_file(path: Path, payload):
    with FileLock(path):
        atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=True))


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


def _new_session_id() -> str:
    return f"session-{uuid.uuid4().hex[:12]}"


def _default_stats_state() -> dict:
    return {
        "live_stats_reset_at": None,
    }


def _default_session_state(mode: str = "dry-run", service_name: str = "Unknown", bank_start: float = 100.0) -> dict:
    return {
        "session_id": _new_session_id(),
        "started_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_bank_start": float(bank_start),
        "mode_at_session_start": mode,
        "service_name_at_session_start": service_name,
        "roi_40_alert_sent": False,
    }


def _resolve_session_bank_start(mode: str, desired_mode: str, settings: dict, account: dict | None = None) -> float:
    account = account or {}
    if mode == "live" or (mode == "Unknown" and desired_mode == "live"):
        portfolio = _safe_float(account.get("portfolio"))
        cash = _safe_float(account.get("cash"))
        if portfolio is not None:
            return float(portfolio)
        if cash is not None:
            return float(cash)
    return float(settings.get("sim_bank", settings.get("bank", 100.0)) or 100.0)


def load_stats_state() -> dict:
    data = _load_json_file(STATS_STATE_FILE, _default_stats_state())
    if not isinstance(data, dict):
        return _default_stats_state()
    payload = _default_stats_state()
    payload.update(data)
    return payload


def save_stats_state(state: dict):
    payload = _default_stats_state()
    payload.update(state or {})
    _save_json_file(STATS_STATE_FILE, payload)


def load_session_state() -> dict:
    data = _load_json_file(SESSION_STATE_FILE, None)
    if not isinstance(data, dict):
        return {}
    return data


def save_session_state(state: dict):
    _save_json_file(SESSION_STATE_FILE, state)


def ensure_session_state(settings: dict, runtime_state: dict | None = None, account: dict | None = None) -> dict:
    runtime_state = runtime_state or {}
    session = load_session_state()
    if session.get("session_id"):
        return session
    actual_mode = str(runtime_state.get("mode") or "Unknown")
    desired_mode = str(settings.get("desired_mode", "dry-run") or "dry-run")
    service_name = str(runtime_state.get("service_name") or "Unknown")
    bank_start = _resolve_session_bank_start(actual_mode, desired_mode, settings, account)
    session = _default_session_state(
        mode=actual_mode if actual_mode != "Unknown" else desired_mode,
        service_name=service_name,
        bank_start=bank_start,
    )
    save_session_state(session)
    return session


def create_new_session(settings: dict, runtime_state: dict, account: dict | None = None) -> dict:
    actual_mode = str(runtime_state.get("mode") or "Unknown")
    desired_mode = str(settings.get("desired_mode", "dry-run") or "dry-run")
    service_name = str(runtime_state.get("service_name") or "Unknown")
    bank_start = _resolve_session_bank_start(actual_mode, desired_mode, settings, account)
    session = _default_session_state(
        mode=actual_mode if actual_mode != "Unknown" else desired_mode,
        service_name=service_name,
        bank_start=bank_start,
    )
    save_session_state(session)
    return session

# ===== DATA HELPERS — runtime data =====
@st.cache_data(ttl=10, show_spinner=False)
def load_saved_signals():
    """Load all saved signals from signals.json."""
    try:
        data = json.loads(SIGNALS_FILE.read_text())
        return data if isinstance(data, list) else []
    except:
        return []


@st.cache_data(ttl=15, show_spinner=False)
def load_window_samples():
    """Load full-window observation samples."""
    try:
        if WINDOW_SAMPLES_JSONL_FILE.exists():
            tail = deque(maxlen=50000)
            with WINDOW_SAMPLES_JSONL_FILE.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    tail.append(line)
            samples = []
            for raw in tail:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    samples.append(obj)
            return samples
        data = json.loads(WINDOW_SAMPLES_FILE.read_text())
        return data if isinstance(data, list) else []
    except:
        return []


@st.cache_data(ttl=30, show_spinner=False)
def load_core_ev_rules():
    """Load runtime Core EV rulebook metadata."""
    try:
        data = json.loads(CORE_EV_RULES_FILE.read_text())
        return data if isinstance(data, dict) else {"buckets": {}}
    except:
        return {"buckets": {}}


def _safe_float(value):
    try:
        if isinstance(value, bool):
            return None
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def parse_ts(value: str):
    try:
        if not value:
            return None
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _normalize_usdc_amount(value):
    """Normalize raw USDC values when APIs return 6-decimal base units."""
    if value is None:
        return None
    if abs(value) >= 100000:
        return value / 1_000_000
    return value


def _coerce_payload_rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "rows", "results", "positions"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
        return [payload]
    return []


def _first_present_amount(obj: dict, *keys: str):
    for key in keys:
        value = _normalize_usdc_amount(_safe_float(obj.get(key)))
        if value is not None:
            return value
    return None


def _looks_like_valid_portfolio(value, cash, positions_value):
    if value is None:
        return False
    if cash is None and positions_value is None:
        return value >= 0
    expected_floor = max(cash or 0, 0)
    expected_total = (cash or 0) + (positions_value or 0)
    tolerance = 0.05
    return value + tolerance >= expected_floor and value <= expected_total + tolerance


def _fmt_money_or_unknown(value):
    if value is None:
        return "Unknown"
    return f"${float(value):.2f}"


def _infer_mode_from_execstart(exec_start: str | None, fallback: str = "Unknown") -> str:
    text = str(exec_start or "").lower()
    if "--dry-run" in text or "test" in text:
        return "dry-run"
    if "--paper" in text:
        return "paper"
    if "--live" in text:
        return "live"
    return fallback or "Unknown"


def _extract_log_path_from_systemd(value: str | None) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    for prefix in ("append:", "file:"):
        if text.startswith(prefix):
            raw_path = text[len(prefix):].strip()
            return Path(raw_path) if raw_path else None
    return None


def _active_bot_services(services: list[dict]) -> list[dict]:
    return [
        row for row in services
        if row.get("load_state") != "not-found" and row.get("active_state") == "active"
    ]


@st.cache_data(ttl=5, show_spinner=False)
def get_runtime_service_state():
    """Resolve the currently running bot service via systemd, not via a single log file."""
    service_rows = []
    for candidate in BOT_SERVICE_CANDIDATES:
        service_name = candidate["name"]
        fallback_mode = candidate["default_mode"]
        fallback_log = candidate["default_log"]
        row = {
            "service_name": service_name,
            "mode": fallback_mode,
            "log_path": str(fallback_log),
            "active_state": "Unknown",
            "sub_state": "Unknown",
            "load_state": "Unknown",
            "active_enter_ts": "",
            "active_enter_mono": 0,
            "exec_start": "",
            "fragment_path": "",
        }
        try:
            proc = subprocess.run(
                [
                    "systemctl",
                    "show",
                    service_name,
                    "--property=Id,LoadState,ActiveState,SubState,ExecStart,StandardOutput,StandardError,FragmentPath,ActiveEnterTimestamp,ActiveEnterTimestampMonotonic",
                    "--no-pager",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0:
                data = {}
                for line in (proc.stdout or "").splitlines():
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    data[key] = value.strip()
                row["load_state"] = data.get("LoadState") or "Unknown"
                row["active_state"] = data.get("ActiveState") or "Unknown"
                row["sub_state"] = data.get("SubState") or "Unknown"
                row["active_enter_ts"] = data.get("ActiveEnterTimestamp") or ""
                row["exec_start"] = data.get("ExecStart") or ""
                row["fragment_path"] = data.get("FragmentPath") or ""
                try:
                    row["active_enter_mono"] = int(data.get("ActiveEnterTimestampMonotonic") or 0)
                except Exception:
                    row["active_enter_mono"] = 0

                stdout_log = _extract_log_path_from_systemd(data.get("StandardOutput"))
                stderr_log = _extract_log_path_from_systemd(data.get("StandardError"))
                log_path = stdout_log or stderr_log or fallback_log
                row["log_path"] = str(log_path)
                row["mode"] = _infer_mode_from_execstart(row["exec_start"], fallback_mode)
        except Exception:
            pass
        service_rows.append(row)

    active_services = _active_bot_services(service_rows)
    selected = None
    if active_services:
        selected = max(active_services, key=lambda row: (int(row.get("active_enter_mono") or 0), row.get("service_name") or ""))
    else:
        known_services = [row for row in service_rows if row.get("load_state") != "not-found"]
        if known_services:
            selected = max(known_services, key=lambda row: (int(row.get("active_enter_mono") or 0), row.get("service_name") or ""))

    if not selected:
        return {
            "service_name": "Unknown",
            "mode": "Unknown",
            "log_path": None,
            "active_state": "Unknown",
            "sub_state": "Unknown",
            "session_start_ts": None,
            "session_bank_start": None,
            "state": "start",
            "sleep": 0,
            "nc": "--:--",
            "round": 0,
            "err": 0,
            "services": service_rows,
        }

    parsed = parse_log_state(Path(selected["log_path"]) if selected.get("log_path") else None)
    parsed["mode"] = selected.get("mode") or "Unknown"
    parsed["service_name"] = selected.get("service_name") or "Unknown"
    parsed["log_path"] = selected.get("log_path")
    parsed["active_state"] = selected.get("active_state") or "Unknown"
    parsed["sub_state"] = selected.get("sub_state") or "Unknown"
    parsed["services"] = service_rows
    parsed["active_service_count"] = len(active_services)
    parsed["has_service_conflict"] = len(active_services) > 1
    return parsed


def _normalize_wallet_address(value):
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", text):
        return text
    return None


def _rpc_hex_to_int(value):
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text.startswith("0x"):
        return None
    try:
        return int(text, 16)
    except Exception:
        return None


def _get_polygon_rpc_urls() -> list[str]:
    raw_values = [
        os.getenv("POLYGON_RPC_URL", ""),
        os.getenv("POLYGON_RPC_URLS", ""),
    ]
    urls = []
    for raw in raw_values:
        if not raw:
            continue
        for part in re.split(r"[\s,;]+", raw.strip()):
            url = part.strip()
            if url and url not in urls:
                urls.append(url)
    return urls


def _fetch_erc20_balance(wallet: str, token_address: str, rpc_url: str) -> float | None:
    wallet_addr = _normalize_wallet_address(wallet)
    token_addr = _normalize_wallet_address(token_address)
    if not wallet_addr or not token_addr or not rpc_url:
        return None

    call_data = "0x70a08231" + wallet_addr[2:].lower().rjust(64, "0")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {
                "to": token_addr,
                "data": call_data,
            },
            "latest",
        ],
    }

    try:
        resp = requests.post(rpc_url, json=payload, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        raw = _rpc_hex_to_int(data.get("result"))
        if raw is None:
            return None
        return raw / 1_000_000
    except Exception:
        return None


def _fetch_erc20_balance_diagnostic(wallet: str, token_address: str, rpc_url: str) -> tuple[float | None, str | None]:
    wallet_addr = _normalize_wallet_address(wallet)
    token_addr = _normalize_wallet_address(token_address)
    if not wallet_addr:
        return None, "Invalid wallet address"
    if not token_addr:
        return None, "Invalid token address"
    if not rpc_url:
        return None, "Missing POLYGON_RPC_URL"

    call_data = "0x70a08231" + wallet_addr[2:].lower().rjust(64, "0")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {
                "to": token_addr,
                "data": call_data,
            },
            "latest",
        ],
    }

    try:
        resp = requests.post(rpc_url, json=payload, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return None, f"pUSD RPC request failed for {wallet_addr}: {exc}"

    if isinstance(data, dict) and data.get("error"):
        return None, f"pUSD RPC returned error for {wallet_addr}: {data.get('error')}"

    raw = _rpc_hex_to_int(data.get("result") if isinstance(data, dict) else None)
    if raw is None:
        return None, f"pUSD RPC returned invalid balance payload for {wallet_addr}: {data}"

    return raw / 1_000_000, None


def _fetch_polymarket_pusd_balance(wallet: str) -> float | None:
    for rpc_url in _get_polygon_rpc_urls():
        balance = _fetch_erc20_balance(wallet, PUSD_TOKEN, rpc_url)
        if balance is not None:
            return balance
    return None


def _fetch_polymarket_pusd_balance_diagnostic(wallet: str) -> tuple[float | None, str | None]:
    rpc_urls = _get_polygon_rpc_urls()
    if not rpc_urls:
        return None, "Missing POLYGON_RPC_URL/POLYGON_RPC_URLS"

    errors = []
    for rpc_url in rpc_urls:
        balance, error = _fetch_erc20_balance_diagnostic(wallet, PUSD_TOKEN, rpc_url)
        if balance is not None:
            return balance, None
        if error:
            errors.append(f"{rpc_url} -> {error}")

    return None, "; ".join(errors[:3]) if errors else "Unknown Polygon RPC error"


def _extract_wallet_candidates(payload) -> list[str]:
    found = []

    def add(value):
        addr = _normalize_wallet_address(value)
        if addr and addr not in found:
            found.append(addr)

    def walk(value):
        if isinstance(value, dict):
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)
        elif isinstance(value, str):
            add(value)
            for match in re.findall(r"0x[a-fA-F0-9]{40}", value):
                add(match)

    walk(payload)
    return found


def _resolve_polymarket_addresses(seed_addresses: list[str]) -> list[str]:
    addresses = []
    for value in seed_addresses:
        addr = _normalize_wallet_address(value)
        if addr and addr not in addresses:
            addresses.append(addr)

    discovered = list(addresses)
    for address in discovered:
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/public-profile",
                params={"wallet": address},
                timeout=5,
            )
            resp.raise_for_status()
            for candidate in _extract_wallet_candidates(resp.json()):
                if candidate not in addresses:
                    addresses.append(candidate)
        except Exception:
            continue
    return addresses


def _fetch_polymarket_user_snapshot(user_address: str) -> dict:
    snapshot = {
        "cash": None,
        "portfolio": None,
        "redeemable": None,
        "positions_value": None,
        "open_positions": None,
        "wallet": user_address,
        "source_error": None,
    }
    portfolio_from_value = None

    try:
        value_resp = requests.get(
            f"{POLYMARKET_DATA_API}/value",
            params={"user": user_address},
            timeout=5,
        )
        value_resp.raise_for_status()
        payload = value_resp.json()
        value_rows = _coerce_payload_rows(payload)
        for row in value_rows:
            if not isinstance(row, dict):
                continue
            portfolio = _first_present_amount(
                row,
                "value",
                "currentValue",
                "portfolioValue",
                "totalValue",
                "usdValue",
            )
            if portfolio is not None:
                portfolio_from_value = portfolio
                break
    except Exception as exc:
        snapshot["source_error"] = str(exc)

    try:
        positions_resp = requests.get(
            f"{POLYMARKET_DATA_API}/positions",
            params={"user": user_address, "sizeThreshold": 0},
            timeout=5,
        )
        positions_resp.raise_for_status()
        payload = positions_resp.json()
        positions = _coerce_payload_rows(payload)

        if isinstance(positions, list):
            redeemable = 0.0
            positions_value = 0.0
            open_positions = 0
            has_redeemable = False
            has_positions_value = False

            for pos in positions:
                if not isinstance(pos, dict):
                    continue

                current_value = _first_present_amount(
                    pos,
                    "currentValue",
                    "value",
                    "portfolioValue",
                    "usdValue",
                    "amountValue",
                )
                if current_value is not None:
                    positions_value += current_value
                    if current_value > 0:
                        open_positions += 1
                    has_positions_value = True

                redeem_value = _first_present_amount(
                    pos,
                    "redeemableValue",
                    "redeemedValue",
                    "claimableValue",
                    "claimable",
                )
                if redeem_value is None and pos.get("redeemable") is True:
                    redeem_value = current_value
                if redeem_value is None and pos.get("redeemable") is True:
                    redeem_value = _first_present_amount(pos, "size", "balance")
                if redeem_value is not None:
                    redeemable += redeem_value
                    has_redeemable = True

            if has_positions_value:
                snapshot["positions_value"] = positions_value
            snapshot["open_positions"] = open_positions
            if has_redeemable:
                snapshot["redeemable"] = redeemable
    except Exception as exc:
        if snapshot["source_error"] is None:
            snapshot["source_error"] = str(exc)

    if _looks_like_valid_portfolio(portfolio_from_value, snapshot["cash"], snapshot["positions_value"]):
        snapshot["portfolio"] = portfolio_from_value
    else:
        snapshot["portfolio"] = snapshot["positions_value"]

    if snapshot["redeemable"] is None and snapshot["positions_value"] is not None:
        snapshot["redeemable"] = 0.0

    return snapshot


def _score_polymarket_snapshot(snapshot: dict) -> tuple:
    portfolio = _safe_float(snapshot.get("portfolio")) or 0.0
    cash = _safe_float(snapshot.get("cash")) or 0.0
    positions_value = _safe_float(snapshot.get("positions_value")) or 0.0
    redeemable = _safe_float(snapshot.get("redeemable")) or 0.0
    open_positions = int(snapshot.get("open_positions") or 0)
    has_error = 1 if snapshot.get("source_error") else 0
    return (
        portfolio > 0,
        cash > 0,
        positions_value > 0,
        redeemable > 0,
        open_positions,
        portfolio,
        cash,
        positions_value,
        -has_error,
    )


def _derive_polymarket_v2_creds(client):
    for method_name in ("create_or_derive_api_key", "create_api_key", "create_or_derive_api_creds"):
        method = getattr(client, method_name, None)
        if not callable(method):
            continue
        creds = method()
        if creds:
            return creds
    return None


def _build_legacy_polymarket_client(private_key: str, proxy_wallet: str):
    import importlib

    client_module = importlib.import_module("py_clob_client.client")
    ClobClient = client_module.ClobClient
    signature_type = _get_polymarket_signature_type(proxy_wallet)

    return ClobClient(
        host=CLOB_API,
        key=private_key,
        chain_id=137,
        signature_type=signature_type,
        funder=proxy_wallet,
    )


def _legacy_creds_to_v2_api_creds(private_key: str, proxy_wallet: str, api_creds_cls):
    try:
        legacy_client = _build_legacy_polymarket_client(private_key, proxy_wallet)
        legacy_creds = legacy_client.create_or_derive_api_creds()
        if not legacy_creds:
            return None

        api_key = getattr(legacy_creds, "api_key", None)
        api_secret = getattr(legacy_creds, "api_secret", None)
        api_passphrase = getattr(legacy_creds, "api_passphrase", None)
        if not (api_key and api_secret and api_passphrase):
            return None

        return api_creds_cls(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
    except Exception:
        return None


def _get_polymarket_signature_type(proxy_wallet: str) -> int:
    signature_type_raw = os.getenv("POLY_SIGNATURE_TYPE")
    if signature_type_raw is not None:
        return int(signature_type_raw)
    return 2 if proxy_wallet else 0


def _build_polymarket_v2_client(private_key: str, proxy_wallet: str, creds=None):
    import importlib

    client_module = importlib.import_module("py_clob_client_v2")
    ClobClient = client_module.ClobClient
    signature_type = _get_polymarket_signature_type(proxy_wallet)

    constructor_variants = [
        {"host": CLOB_API, "chain_id": 137, "key": private_key, "creds": creds, "signature_type": signature_type, "funder": proxy_wallet or None},
        {"host": CLOB_API, "chain_id": 137, "key": private_key, "creds": creds, "signature_type": signature_type, "funder_address": proxy_wallet or None},
        {"host": CLOB_API, "chain_id": 137, "key": private_key, "creds": creds, "funder": proxy_wallet or None},
        {"host": CLOB_API, "chain_id": 137, "key": private_key, "creds": creds, "funder_address": proxy_wallet or None},
        {"host": CLOB_API, "chain_id": 137, "key": private_key, "creds": creds, "signature_type": signature_type},
        {"host": CLOB_API, "chain_id": 137, "key": private_key, "creds": creds},
    ]

    last_error = None
    for kwargs in constructor_variants:
        clean_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            return ClobClient(**clean_kwargs)
        except TypeError as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise RuntimeError("Unable to initialize py_clob_client_v2 ClobClient")


def _get_v2_collateral_balance_allowance(private_key: str, proxy_wallet: str) -> tuple[float | None, float | None]:
    """Return collateral balance/allowance using py_clob_client_v2 when available."""
    try:
        import importlib

        client_module = importlib.import_module("py_clob_client_v2")
        BalanceAllowanceParams = client_module.BalanceAllowanceParams
        AssetType = client_module.AssetType
        ApiCreds = getattr(client_module, "ApiCreds", None)

        env_api_key = os.getenv("CLOB_API_KEY", "")
        env_api_secret = os.getenv("CLOB_SECRET", "")
        env_api_passphrase = os.getenv("CLOB_PASS_PHRASE", "")

        creds = None
        if ApiCreds and env_api_key and env_api_secret and env_api_passphrase:
            creds = ApiCreds(
                api_key=env_api_key,
                api_secret=env_api_secret,
                api_passphrase=env_api_passphrase,
            )
        elif ApiCreds:
            creds = _legacy_creds_to_v2_api_creds(private_key, proxy_wallet, ApiCreds)

        client = _build_polymarket_v2_client(private_key, proxy_wallet, creds=creds)
        if creds is None:
            derived_creds = _derive_polymarket_v2_creds(client)
            if derived_creds:
                client = _build_polymarket_v2_client(private_key, proxy_wallet, creds=derived_creds)

        raw = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
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


def get_collateral_balance_allowance(private_key: str, proxy_wallet: str) -> tuple[float | None, float | None]:
    """Return available collateral balance and allowance from Polymarket, if available."""
    v2_balance, v2_allowance = _get_v2_collateral_balance_allowance(private_key, proxy_wallet)
    if v2_balance is not None or v2_allowance is not None:
        return v2_balance, v2_allowance

    try:
        import importlib

        clob_types_module = importlib.import_module("py_clob_client.clob_types")

        BalanceAllowanceParams = clob_types_module.BalanceAllowanceParams
        AssetType = clob_types_module.AssetType

        client = _build_legacy_polymarket_client(private_key, proxy_wallet)
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


@st.cache_data(ttl=15, show_spinner=False)
def fetch_polymarket_account_state() -> dict:
    """Fetch real Polymarket cash and portfolio values for dashboard display."""
    private_key = os.getenv("POLY_PRIVATE_KEY", "")
    proxy_wallet = os.getenv("POLY_PROXY_WALLET", "")
    poly_wallet = os.getenv("POLY_WALLET", "")
    user_address = proxy_wallet or poly_wallet

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

    candidate_addresses = _resolve_polymarket_addresses([proxy_wallet, poly_wallet])
    best_pusd_cash = None
    pusd_notes = []
    for candidate in candidate_addresses:
        candidate_pusd_cash, candidate_pusd_error = _fetch_polymarket_pusd_balance_diagnostic(candidate)
        if candidate_pusd_cash is not None and (best_pusd_cash is None or candidate_pusd_cash > best_pusd_cash):
            best_pusd_cash = candidate_pusd_cash
            result["wallet"] = candidate
        if candidate_pusd_error:
            pusd_notes.append(candidate_pusd_error)

    if best_pusd_cash is not None and (result["cash"] is None or result["cash"] <= 0):
        result["cash"] = best_pusd_cash
        if result["allowance"] is not None:
            result["spendable"] = min(best_pusd_cash, result["allowance"])
        else:
            result["spendable"] = best_pusd_cash

    best_snapshot = None
    for candidate in candidate_addresses:
        snapshot = _fetch_polymarket_user_snapshot(candidate)
        if best_snapshot is None or _score_polymarket_snapshot(snapshot) > _score_polymarket_snapshot(best_snapshot):
            best_snapshot = snapshot

    if best_snapshot is not None:
        result["wallet"] = best_snapshot.get("wallet") or result["wallet"]
        result["positions_value"] = best_snapshot.get("positions_value")
        result["open_positions"] = best_snapshot.get("open_positions")
        result["redeemable"] = best_snapshot.get("redeemable")
        positions_value = best_snapshot.get("positions_value")
        fallback_portfolio = None
        if result["cash"] is not None or positions_value is not None:
            fallback_portfolio = (result["cash"] or 0) + (positions_value or 0)

        if _looks_like_valid_portfolio(best_snapshot.get("portfolio"), result["cash"], positions_value):
            result["portfolio"] = best_snapshot.get("portfolio")
        else:
            result["portfolio"] = fallback_portfolio

        if result["redeemable"] is None:
            result["redeemable"] = 0.0 if positions_value is not None else None
        if result["source_error"] is None:
            result["source_error"] = best_snapshot.get("source_error")

    if result["source_error"] is None and pusd_notes and best_pusd_cash is None:
        result["source_error"] = "; ".join(pusd_notes[:3])

    return result

@st.cache_data(ttl=10, show_spinner=False)
def build_dashboard_state(
    session_start_ts: str | None = None,
    session_bank_start: float | None = None,
    session_id: str | None = None,
    actual_mode: str | None = None,
    stats_reset_at: str | None = None,
):
    """
    Строим состояние Dashboard из signals.json + window_samples.json.
    Возвращает dict с метриками, последними сигналами, full-window наблюдениями и rulebook state.
    """
    signals = load_saved_signals()
    window_samples = load_window_samples()
    core_ev_rules = load_core_ev_rules()
    session_start_dt = parse_ts(session_start_ts) if session_start_ts else None
    stats_reset_dt = parse_ts(stats_reset_at) if stats_reset_at else None
    all_signals = list(signals)

    if session_id:
        session_signals = [s for s in signals if str(s.get("session_id", "") or "") == session_id]
        session_window_samples = [s for s in window_samples if str(s.get("session_id", "") or "") == session_id]
    else:
        session_signals = list(signals)
        session_window_samples = list(window_samples)
        if session_start_dt is not None:
            session_signals = [s for s in session_signals if (parse_ts(str(s.get("timestamp", "") or "")) or datetime.min) >= session_start_dt]
            session_window_samples = [s for s in session_window_samples if (parse_ts(str(s.get("timestamp", "") or "")) or datetime.min) >= session_start_dt]

    signals = session_signals
    window_samples = session_window_samples

    # Разделяем на вошедшие и пропущенные
    entered = [s for s in signals if s.get("entered")]
    skipped = [s for s in signals if not s.get("entered")]
    resolved_window_samples = [s for s in window_samples if s.get("pnl_if_entered") is not None]
    settled_trade_rows = []
    last_window_samples = window_samples[-20:]
    full_window_waits = [s for s in signals if str(s.get("reason", "")).startswith("full window wait |")]
    last_full_window_waits = full_window_waits[-12:]
    last_core_ev_signals = [s for s in signals if s.get("core_ev_decision")][-10:]

    core_ev_runtime_decisions_by_n = {}
    core_ev_recent_rows_by_n = {}
    for window_size in (50, 100, 200):
        decision_counts = {"strong_allow": 0, "allow": 0, "micro_allow": 0, "watch": 0, "deny": 0, "unknown": 0, "other": 0}
        recent_rows = []
        scoped_signals = signals[-window_size:]
        for s in scoped_signals:
            decision = str(s.get("core_ev_decision", "unknown") or "unknown")
            if decision in decision_counts:
                decision_counts[decision] += 1
            else:
                decision_counts["other"] += 1
        for s in reversed([x for x in scoped_signals if x.get("core_ev_decision")][-10:]):
            recent_rows.append({
                "Time": s.get("timestamp", ""),
                "Decision": str(s.get("core_ev_decision", "") or ""),
                "Level": str(s.get("core_ev_bucket_level", "") or ""),
                "Time Left": f"{float(s.get('time_left', 0) or 0):.1f}s",
                "Reason": str(s.get("core_ev_reason", s.get("reason", "")) or "")[:84],
            })
        core_ev_runtime_decisions_by_n[window_size] = decision_counts
        core_ev_recent_rows_by_n[window_size] = recent_rows

    core_ev_decision_counts = core_ev_runtime_decisions_by_n[200]
    core_ev_recent_rows = core_ev_recent_rows_by_n[200]

    bucket_rows = [{"key": key, **stats} for key, stats in (core_ev_rules.get("buckets", {}) or {}).items() if isinstance(stats, dict)]
    rulebook_decisions = {"strong_allow": 0, "allow": 0, "watch": 0, "deny": 0, "unknown": 0, "other": 0}
    for row in bucket_rows:
        decision = str(row.get("decision", "unknown") or "unknown")
        if decision in rulebook_decisions:
            rulebook_decisions[decision] += 1
        else:
            rulebook_decisions["other"] += 1

    bucket_level_counts = {"L1": 0, "L2": 0, "L3": 0, "trend_conflict": 0, "high_pm_micro": 0, "other": 0}
    for s in signals[-200:]:
        level = str(s.get("core_ev_bucket_level", "") or "other")
        if level in bucket_level_counts:
            bucket_level_counts[level] += 1
        else:
            bucket_level_counts["other"] += 1

    time_bucket_counts = {}
    for s in window_samples[-200:]:
        t = _safe_float(s.get("time_left"))
        if t is None:
            key = "unknown"
        elif t < 10:
            key = "<10s"
        elif t < 20:
            key = "10-19s"
        elif t < 30:
            key = "20-29s"
        elif t < 60:
            key = "30-59s"
        elif t < 120:
            key = "60-119s"
        else:
            key = "120-300s"
        time_bucket_counts[key] = time_bucket_counts.get(key, 0) + 1

    top_allow_buckets = [row for row in bucket_rows if row.get("decision") in {"allow", "strong_allow"}]
    top_allow_buckets.sort(key=lambda row: (float(row.get("roi", 0) or 0), int(row.get("trades", 0) or 0)), reverse=True)
    top_deny_buckets = [row for row in bucket_rows if row.get("decision") == "deny"]
    top_deny_buckets.sort(key=lambda row: (float(row.get("roi", 0) or 0), -int(row.get("trades", 0) or 0)))

    core_ev_deny_reasons = {}
    for s in signals:
        if str(s.get("core_ev_decision", "") or "") != "deny":
            continue
        reason = str(s.get("core_ev_reason", s.get("reason", "other")) or "other").lower()
        if "pm outside flexible core ev zone" in reason:
            key = "pm outside flex zone"
        elif "aligned non-conflicting trend" in reason or "trend conflict" in reason:
            key = "trend conflict"
        elif "l1 fallback" in reason or "below l2+ specificity" in reason or "requires l2" in reason:
            key = "l1/l2/l3 specificity"
        elif "undersampled" in reason or "unknown core ev bucket" in reason:
            key = "undersampled / unknown"
        elif "historically negative" in reason:
            key = "historically negative bucket"
        elif "shadow live deny" in reason:
            key = "shadow live deny"
        else:
            key = "other"
        core_ev_deny_reasons[key] = core_ev_deny_reasons.get(key, 0) + 1

    # Считаем invested и pnl из вошедших сигналов
    settings = load_settings()
    amount = settings.get("amount", 10)
    bank_start = float(session_bank_start if session_bank_start is not None else settings.get("sim_bank", settings.get("bank", 100)))
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

    for s in reversed(entered):
        realized_pnl_value = s.get("realized_pnl")
        if realized_pnl_value is None:
            continue
        entry_time = str(s.get("entry_time", s.get("timestamp", "")) or "")
        close_time = str(s.get("close_time", "") or "")
        if not close_time:
            close_ts = s.get("market_close_ts")
            if close_ts is not None:
                try:
                    close_time = datetime.fromtimestamp(int(close_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    close_time = ""
        decision = str(s.get("core_ev_decision", "") or "")
        level = str(s.get("core_ev_bucket_level", "") or "")
        if not level and decision:
            level = "runtime"
        outcome = str(s.get("outcome", s.get("result", "PENDING")) or "PENDING")
        exec_status = str(s.get("exec_status", "") or "")
        if not exec_status:
            if s.get("entered"):
                exec_status = "success"
            elif s.get("execution_failure_type"):
                exec_status = str(s.get("execution_failure_type", "") or "")
            elif s.get("execution_order_status"):
                exec_status = str(s.get("execution_order_status", "") or "")
            else:
                exec_status = "unknown"
        settled_trade_rows.append({
            "Entry Time": entry_time,
            "Close Time": close_time or "—",
            "Decision": decision or "—",
            "Level": level or "—",
            "PM": f"{float(s.get('pm', 0) or 0):.3f}",
            "Delta": f"{float(s.get('delta', 0) or 0):.4f}%",
            "Time Left": f"{float(s.get('time_left', 0) or 0):.1f}s",
            "Side": str(s.get("side", "") or "—"),
            "Amount": f"{float(s.get('amount', 0) or 0):.2f}",
            "PnL": f"{float(realized_pnl_value or 0):+.2f}",
            "Outcome": outcome,
            "Reason": str(s.get("core_ev_reason", s.get("reason", "")) or ""),
            "Exec Status": exec_status,
        })

    l1_fallback_signals = [
        s for s in signals
        if "full-window L1 fallback" in str(s.get("core_ev_reason", "") or "")
    ]
    l1_fallback_resolved = [s for s in l1_fallback_signals if s.get("realized_pnl") is not None]
    l1_fallback_avg_roi = None
    if l1_fallback_resolved:
        l1_fallback_avg_roi = sum(float(s.get("realized_pnl", 0) or 0) for s in l1_fallback_resolved) / len(l1_fallback_resolved)

    micro_allow_signals = [s for s in signals if str(s.get("core_ev_decision", "") or "") == "micro_allow"]
    micro_allow_entered = [s for s in micro_allow_signals if s.get("entered")]
    micro_allow_resolved = [s for s in micro_allow_entered if s.get("realized_pnl") is not None]
    micro_allow_wins = len([s for s in micro_allow_resolved if s.get("won") == True])
    micro_allow_avg_roi = None
    if micro_allow_resolved:
        micro_allow_avg_roi = sum(float(s.get("realized_pnl", 0) or 0) for s in micro_allow_resolved) / len(micro_allow_resolved)

    wait_slugs = {str(s.get("market_slug", "") or "") for s in full_window_waits if s.get("market_slug")}
    wait_enter_count = len([s for s in entered if str(s.get("market_slug", "") or "") in wait_slugs])

    realized_pnl_by_bucket_level = {}
    for s in entered:
        if s.get("realized_pnl") is None:
            continue
        level = str(s.get("core_ev_bucket_level", "unknown") or "unknown")
        realized_pnl_by_bucket_level[level] = realized_pnl_by_bucket_level.get(level, 0.0) + float(s.get("realized_pnl", 0) or 0)

    realized_pnl_by_decision = {}
    for s in entered:
        if s.get("realized_pnl") is None:
            continue
        decision = str(s.get("core_ev_decision", "unknown") or "unknown")
        realized_pnl_by_decision[decision] = realized_pnl_by_decision.get(decision, 0.0) + float(s.get("realized_pnl", 0) or 0)

    win_rate_by_time_bucket = {}
    for s in entered:
        if s.get("won") is None:
            continue
        t = _safe_float(s.get("time_left"))
        if t is None:
            key = "unknown"
        elif t < 20:
            key = "10-19s"
        elif t < 30:
            key = "20-29s"
        elif t < 60:
            key = "30-59s"
        elif t < 120:
            key = "60-119s"
        else:
            key = "120-300s"
        row = win_rate_by_time_bucket.setdefault(key, {"wins": 0, "losses": 0})
        if s.get("won") == True:
            row["wins"] += 1
        elif s.get("won") == False:
            row["losses"] += 1

    # Skip reasons breakdown
    skip_reasons = {}
    for s in skipped:
        reason = s.get("reason", "other")
        coin = str(s.get("coin", "") or "").upper()
        # Нормализуем причины
        if "btc_low" in reason.lower() or ("PM price <" in reason and coin == "BTC"):
            key = "btc_low"
        elif "eth_low" in reason.lower() or ("PM price <" in reason and coin == "ETH"):
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
    shadow_live_signals = [
        s for s in signals
        if s.get("shadow_live_decision") is not None or s.get("shadow_live_mode") is not None
    ]
    last_shadow_live = shadow_live_signals[-12:]

    shadow_live_counts = {"strong_allow": 0, "allow": 0, "watch": 0, "deny": 0, "neutral": 0, "other": 0}
    for s in shadow_live_signals[-100:]:
        decision = str(s.get("shadow_live_decision", "other") or "other")
        if decision in shadow_live_counts:
            shadow_live_counts[decision] += 1
        else:
            shadow_live_counts["other"] += 1

    shadow_live_mode = "unknown"
    if shadow_live_signals:
        shadow_live_mode = str(shadow_live_signals[-1].get("shadow_live_mode", "unknown") or "unknown")

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
    active_mode = str(actual_mode or "Unknown")
    if active_mode != "live":
        account_state = {
            **account_state,
            "cash": bank_current,
            "portfolio": bank_current,
            "spendable": bank_current,
            "source_error": "Simulated dry-run balance" if active_mode == "dry-run" else account_state.get("source_error"),
        }

    live_all_time_signals = [s for s in all_signals if str(s.get("mode", "") or "") == "live" and s.get("entered")]
    if stats_reset_dt is not None:
        live_all_time_signals = [
            s for s in live_all_time_signals
            if (parse_ts(str(s.get("timestamp", "") or "")) or datetime.min) >= stats_reset_dt
        ]
    live_all_time_resolved = [s for s in live_all_time_signals if s.get("realized_pnl") is not None]
    live_all_time_wins = len([s for s in live_all_time_resolved if s.get("won") == True])
    live_all_time_losses = len([s for s in live_all_time_resolved if s.get("won") == False])
    live_all_time_pnl = sum(float(s.get("realized_pnl", 0) or 0) for s in live_all_time_resolved)

    return {
        "total_signals": len(signals),
        "total_window_samples": len(window_samples),
        "resolved_window_samples": len(resolved_window_samples),
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
        "last_window_samples": last_window_samples,
        "last_entered": last_entered,
        "last_skipped": last_skipped,
        "last_full_window_waits": last_full_window_waits,
        "settled_trade_rows": settled_trade_rows[:150],
        "core_ev_recent_rows": core_ev_recent_rows,
        "last_shadow_live": last_shadow_live,
        "shadow_live_mode": shadow_live_mode,
        "shadow_live_counts": shadow_live_counts,
        "shadow_live_total": len(shadow_live_signals),
        "core_ev_rules": core_ev_rules,
        "core_ev_rulebook_decisions": rulebook_decisions,
        "core_ev_rulebook_bucket_count": len(bucket_rows),
        "core_ev_source_type": str(core_ev_rules.get("source_type", "unknown") or "unknown"),
        "core_ev_generated_at": str(core_ev_rules.get("generated_at", "unknown") or "unknown"),
        "core_ev_resolved_eligible": int(core_ev_rules.get("resolved_eligible_signals", 0) or 0),
        "core_ev_runtime_decisions": core_ev_decision_counts,
        "core_ev_runtime_decisions_by_n": core_ev_runtime_decisions_by_n,
        "core_ev_deny_reasons": core_ev_deny_reasons,
        "core_ev_recent_rows_by_n": core_ev_recent_rows_by_n,
        "bucket_level_counts": bucket_level_counts,
        "window_time_bucket_counts": time_bucket_counts,
        "top_allow_buckets": top_allow_buckets[:10],
        "top_deny_buckets": top_deny_buckets[:10],
        "l1_fallback_count": len(l1_fallback_signals),
        "l1_fallback_avg_roi": l1_fallback_avg_roi,
        "micro_allow_count": len(micro_allow_signals),
        "micro_allow_entered": len(micro_allow_entered),
        "micro_allow_resolved": len(micro_allow_resolved),
        "micro_allow_wins": micro_allow_wins,
        "micro_allow_avg_roi": micro_allow_avg_roi,
        "wait_enter_count": wait_enter_count,
        "realized_pnl_by_bucket_level": realized_pnl_by_bucket_level,
        "realized_pnl_by_decision": realized_pnl_by_decision,
        "win_rate_by_time_bucket": win_rate_by_time_bucket,
        "btc_signals": [s for s in signals if s.get("coin") == "BTC"],
        "eth_signals": [s for s in signals if s.get("coin") == "ETH"],
        "btc_entered": [s for s in entered if s.get("coin") == "BTC"],
        "eth_entered": [s for s in entered if s.get("coin") == "ETH"],
        "account": account_state,
        "session_start_ts": session_start_ts,
        "session_id": session_id,
        "live_all_time_trades": len(live_all_time_signals),
        "live_all_time_wins": live_all_time_wins,
        "live_all_time_losses": live_all_time_losses,
        "live_all_time_pnl": live_all_time_pnl,
        "time": datetime.now().strftime('%H:%M:%S'),
    }

# Парсинг логов только для статуса (active/sleeping) — не для статистики
_P_STATE = {
    "active": re.compile(r'Active window.*?close\s+([\d:]+)'),
    "sleep":  re.compile(r'Sleeping\s+(\d+)s'),
    "snc":    re.compile(r'next close\s+([\d:]+)'),
    "closed": re.compile(r'Market closed'),
    "startup": re.compile(r'^\[(.*?)\]\s+Crypto Up/Down Bot\s+\|\s+(.*?)\s+\|'),
    "bank": re.compile(r'^\[(.*?)\]\s+Bank:\s+\$([\-\d.]+)'),
}

_LOG_EVENT_PATTERNS = [
    ("core-allow", re.compile(r'CORE[- ]EV.*\b(STRONG_ALLOW|MICRO_ALLOW|ALLOW)\b', re.IGNORECASE), True),
    ("core-deny", re.compile(r'CORE[- ]EV.*\bDENY\b', re.IGNORECASE), True),
    ("core-allow", re.compile(r'\bALLOW\b', re.IGNORECASE), True),
    ("core-deny", re.compile(r'\bDENY\b', re.IGNORECASE), True),
    ("entering", re.compile(r'\bENTERING\b', re.IGNORECASE), True),
    ("win", re.compile(r'\bWIN\b', re.IGNORECASE), True),
    ("loss", re.compile(r'\bLOSS\b', re.IGNORECASE), True),
    ("error", re.compile(r'\b(ERROR|FAILED|EXCEPTION)\b', re.IGNORECASE), True),
    ("entering", re.compile(r'\bNOTE\b|SHADOW-OBSERVE', re.IGNORECASE), False),
]


def _classify_log_line(line: str) -> tuple[str, bool]:
    text = str(line or "")
    for event_type, pattern, is_important in _LOG_EVENT_PATTERNS:
        if pattern.search(text):
            return event_type, is_important
    return "info", False


def _resolve_stats_log_path(runtime_state: dict, settings: dict) -> Path:
    log_path = runtime_state.get("log_path")
    if log_path:
        return Path(log_path)
    desired_mode = str(settings.get("desired_mode") or "dry-run")
    return LIVE_LOG if desired_mode == "live" else DEFAULT_LOG


@st.cache_data(ttl=3, show_spinner=False)
def load_log_tail(path_str: str, limit: int = 150) -> list[str]:
    path = Path(path_str)
    if not path.exists() or not path.is_file():
        return []
    try:
        tail = deque(maxlen=max(int(limit or 150), 1))
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                tail.append(line.rstrip("\r\n"))
        return list(tail)
    except Exception:
        return []


def render_log_lines(lines: list[str], highlight: bool = True):
    if not lines:
        st.caption("Лог пока пуст или ещё не создан.")
        return
    html_lines = []
    for line in lines:
        event_type, _ = _classify_log_line(line)
        css_class = f" log-{event_type}" if highlight and event_type != "info" else ""
        html_lines.append(f"<div class='log-line{css_class}'>{html.escape(line)}</div>")
    st.markdown(f"<div class='log-viewer'>{''.join(html_lines)}</div>", unsafe_allow_html=True)

@st.cache_data(ttl=5, show_spinner=False)
def parse_log_state(path):
    """Парсим логи только для определения текущего состояния бота."""
    lines = []
    candidate = path
    if candidate and candidate.exists():
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            lines = []

    if not lines:
        return {'state': 'start', 'sleep': 0, 'nc': '--:--', 'round': 0, 'err': 0, 'mode': 'Unknown', 'session_start_ts': None, 'session_bank_start': None}

    state = {'state': 'start', 'sleep': 0, 'nc': '--:--', 'round': 0, 'err': 0, 'mode': 'Unknown', 'session_start_ts': None, 'session_bank_start': None}
    startup_idx = None
    for idx, line in enumerate(lines):
        startup_match = _P_STATE["startup"].search(line)
        if startup_match:
            startup_idx = idx
            raw_mode = startup_match.group(2).strip()
            state['session_start_ts'] = startup_match.group(1).strip()
            if 'DRY RUN' in raw_mode.upper():
                state['mode'] = 'dry-run'
            elif 'PAPER' in raw_mode.upper():
                state['mode'] = 'paper'
            elif 'LIVE' in raw_mode.upper():
                state['mode'] = 'live'
            else:
                state['mode'] = raw_mode or 'Unknown'
    if startup_idx is not None:
        for line in lines[startup_idx:startup_idx + 8]:
            bank_match = _P_STATE["bank"].search(line)
            if bank_match:
                state['session_bank_start'] = _safe_float(bank_match.group(2))
                break

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

@st.cache_data(ttl=10, show_spinner=False)
def load_settings():
    """Load bot settings."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            merged = get_default_settings()
            merged.update(data)
            return merged
        return get_default_settings()
    except:
        return get_default_settings()


def _run_systemctl(*args: str, timeout: int = 20) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0:
            return True, f"systemctl {' '.join(args)} ok"
        detail = (proc.stderr or proc.stdout or f"systemctl {' '.join(args)} failed").strip()
        return False, detail
    except Exception as e:
        return False, str(e)


def restart_bot_service(service_name: str = "poly-bot-live.service") -> tuple[bool, str]:
    return _run_systemctl("restart", service_name)


def stop_all_bot_services() -> tuple[bool, str]:
    details = []
    ok = True
    for service_name in ["poly-bot-live.service", "poly-bot-test.service", "poly-bot.service"]:
        result_ok, detail = _run_systemctl("stop", service_name)
        details.append(f"{service_name}: {detail}")
        if not result_ok and "not loaded" not in detail.lower() and "not-found" not in detail.lower():
            ok = False
    return ok, " | ".join(details)


def start_bot_for_mode(desired_mode: str) -> tuple[bool, str]:
    mode = str(desired_mode or "dry-run").strip().lower()
    target_service = "poly-bot-live.service" if mode == "live" else "poly-bot-test.service"
    other_service = "poly-bot-test.service" if target_service == "poly-bot-live.service" else "poly-bot-live.service"
    other_legacy = "poly-bot.service" if target_service != "poly-bot.service" else None
    stop_targets = [other_service]
    if other_legacy:
        stop_targets.append(other_legacy)
    details = []
    for service_name in stop_targets:
        _, detail = _run_systemctl("stop", service_name)
        details.append(f"stop {service_name}: {detail}")
    start_ok, start_detail = _run_systemctl("start", target_service)
    details.append(f"start {target_service}: {start_detail}")
    return start_ok, " | ".join(details)


def wait_for_service_active(service_name: str, timeout: float = 15.0) -> dict:
    started = time.time()
    last_state = get_runtime_service_state()
    while time.time() - started < timeout:
        runtime = get_runtime_service_state()
        last_state = runtime
        if str(runtime.get("service_name") or "") == service_name and runtime.get("active_state") == "active":
            return runtime
        time.sleep(0.5)
        try:
            get_runtime_service_state.clear()
        except Exception:
            pass
    return last_state

def get_default_settings():
    """Возвращает дефолтные настройки бота."""
    return {
        "first_setup_done": False,
        "desired_mode": "dry-run",
        "bank": 100,
        "sim_bank": 100,
        "amount": 5,
        "enabled_coins": ["BTC"],
        "observe_window_seconds": 305,
        "daily_loss_limit": 10.0,
        "daily_loss_limit_pct": 0.20,
        "dynamic_sizing": True,
        "dynamic_min_amount": 5.0,
        "dynamic_max_amount": 15.0,
        "dynamic_base_risk_pct": 0.05,
        "dynamic_step_bank_gain_pct": 0.70,
        "dynamic_step_risk_pct": 0.01,
        "dynamic_max_risk_pct": 0.08,
        "core_ev_enabled": True,
        "core_ev_pm_min": 0.58,
        "core_ev_pm_max": 0.70,
        "core_ev_flex_pm_min": 0.50,
        "core_ev_flex_pm_max": 0.99,
        "core_ev_entry_time_min": 10,
        "core_ev_entry_time_max": 305,
        "core_ev_time_left_min": 10,
        "core_ev_time_left_max": 20,
        "core_ev_max_risk_pct": 0.02,
        "core_ev_micro_risk_pct": 0.005,
        "core_ev_trend_conflict_micro_delta_min_pct": 0.012,
        "core_ev_trend_conflict_micro_confidence_min": 0.0,
        "core_ev_trend_conflict_micro_indicator_min": -0.10,
        "full_window_core_ev_enabled": True,
        "full_window_core_ev_time_left_max": 180,
        "full_window_core_ev_min_level": "L2",
        "full_window_entry_confirm_ticks": 2,
        "full_window_entry_commit_time_left": 19,
        "full_window_entry_min_score_gain": 0.15,
        "full_window_micro_entry_commit_time_left": 30,
        "full_window_l1_fallback_min_trades": 8,
        "full_window_l1_fallback_require_recent_positive": True,
        "full_window_l1_fallback_time_left_max": 150,
        "full_window_l1_strong_exception_min_trades": 2,
        "full_window_l1_strong_exception_min_roi": 50.0,
        "window_sample_logging_enabled": True,
        "trend_conflict_override_delta_min_pct": 0.025,
        "shadow_live_mode": "observe",
    }

def save_settings(settings):
    try:
        merged = get_default_settings()
        merged.update(settings or {})
        _save_json_file(SETTINGS_FILE, merged)
    except: pass

# ===== LABELS для skip reasons =====
RL = {'btc_low': 'BTC below min', 'eth_low': 'ETH below min', 'high': 'Above max',
      'delta': 'Delta too low', 'conf': 'Confidence too low', 'atr': 'ATR filter', 'other': '?'}

# ===== INIT =====
settings = load_settings()
L = get_runtime_service_state()
bot_running = L.get("active_state") == "active" or is_bot_running()
account_state_seed = fetch_polymarket_account_state() if str(L.get("mode") or "Unknown") == "live" else {}
stats_state = load_stats_state()
first_setup_needed = not bool(settings.get("first_setup_done"))
session_state = (
    ensure_session_state(settings, L, account_state_seed)
    if not first_setup_needed
    else _default_session_state(
        mode=str(settings.get("desired_mode", "dry-run") or "dry-run"),
        service_name=str(L.get("service_name") or "Unknown"),
        bank_start=_resolve_session_bank_start(
            str(L.get("mode") or "Unknown"),
            str(settings.get("desired_mode", "dry-run") or "dry-run"),
            settings,
            account_state_seed,
        ),
    )
)
D = build_dashboard_state(
    session_state.get("started_at"),
    session_state.get("session_bank_start"),
    session_state.get("session_id"),
    L.get("mode"),
    stats_state.get("live_stats_reset_at"),
)

# ===== TABS =====
tab_dashboard, tab_stats, tab_settings = st.tabs([
    "📊 Обзор",
    "📈 Статистика",
    "⚙️ Настройки",
])

# ==========================================
# TAB 1: DASHBOARD
# ==========================================
with tab_dashboard:
    if first_setup_needed:
        st.markdown("### 🚀 Первый запуск")
        st.info("Заполните базовые параметры. После сохранения откроется обычный dashboard.")
        wizard_settings = settings.copy()
        w1, w2 = st.columns(2)
        with w1:
            wizard_settings["desired_mode"] = st.selectbox(
                "Режим для запуска бота",
                ["dry-run", "live"],
                index=["dry-run", "live"].index(str(settings.get("desired_mode", "dry-run") or "dry-run")),
            )
            wizard_settings["amount"] = st.number_input(
                "Размер ставки (USDC)",
                min_value=1.0,
                max_value=1000.0,
                value=float(settings.get("amount", 5.0)),
                step=1.0,
            )
        with w2:
            wizard_settings["enabled_coins"] = st.multiselect(
                "Активные монеты",
                ["BTC", "ETH"],
                default=settings.get("enabled_coins", ["BTC"]),
            )
            wizard_settings["sim_bank"] = st.number_input(
                "Симулированный банк dry-run (USDC)",
                min_value=10.0,
                max_value=100000.0,
                value=float(settings.get("sim_bank", settings.get("bank", 100.0))),
                step=10.0,
            )
        if st.button("✅ Завершить первичную настройку", type="primary", width="stretch"):
            wizard_settings["first_setup_done"] = True
            wizard_settings["bank"] = float(wizard_settings.get("sim_bank", 100.0))
            save_settings(wizard_settings)
            create_new_session(wizard_settings, L, account_state_seed)
            st.success("Первичная настройка сохранена.")
            st.rerun()
        st.stop()

    st.markdown("### 📊 Обзор")
    ctrl_cols = st.columns([1, 3, 1])

    with ctrl_cols[0]:
        status_label = "🟢 Running" if bot_running else "🔴 Stopped"
        st.button(status_label, width="stretch", disabled=True)

    with ctrl_cols[1]:
        st.markdown(
            "<div class='safe-box'><strong>Лёгкий обзор бота.</strong> "
            "Ключевые метрики, последние сделки и быстрый refresh без перегруженной аналитики.</div>",
            unsafe_allow_html=True,
        )
        if L.get("has_service_conflict"):
            st.error("Обнаружено несколько активных bot-service одновременно. Dashboard выбрал последний стартовавший сервис, но состояние ненормальное.")

    with ctrl_cols[2]:
        if st.button("🔄 Refresh", width="stretch"):
            st.rerun()
        st.caption(f"Updated: {D['time']}")

    account = D['account']
    mode = str(L.get('mode') or 'Unknown')
    desired_mode = str(settings.get('desired_mode', 'dry-run') or 'dry-run')
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
        ("Desired", desired_mode),
        ("Service", str(L.get('service_name') or 'Unknown')),
        ("Trade", f"${settings.get('amount', 10):.0f}"),
        ("Coins", ', '.join(settings.get('enabled_coins', ['BTC', 'ETH']))),
        ("PID", PID_FILE.read_text().strip() if PID_FILE.exists() else '—'),
    ]
    summary_html = '<div class="summary-bar">'
    for label, value in summary_items:
        summary_html += f'<span class="summary-chip"><strong>{label}</strong> {value}</span>'
    summary_html += '</div>'
    st.markdown(summary_html, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("#### Polymarket")
    pm1, pm2, pm3, pm4 = st.columns(4)
    if mode == "live":
        pm1.metric("💵 Cash", _fmt_money_or_unknown(cash))
        pm2.metric("🧾 Portfolio", _fmt_money_or_unknown(portfolio))
        pm3.metric("💸 Spendable", _fmt_money_or_unknown(spendable))
        pm4.metric("🎁 Redeemable", _fmt_money_or_unknown(redeemable))
    else:
        pm1.metric("💵 Simulated bank", _fmt_money_or_unknown(D['bank_current']))
        pm2.metric("🧾 Session start", _fmt_money_or_unknown(D['bank_start']))
        pm3.metric("💸 Spendable", _fmt_money_or_unknown(D['bank_current']))
        pm4.metric("🎁 Redeemable", "—")
    if account.get('source_error'):
        st.caption(f"Polymarket sync note: {account['source_error']}")

    ico = {'sleeping': '💤', 'active': '⚡', 'start': '🚀'}.get(L['state'], '⚪')
    session_label = L.get('session_start_ts') or 'Unknown'
    st.caption(
        f"{ico} {L['state'].upper()} | Service: {L.get('service_name') or 'Unknown'} | "
        f"Mode: {mode} | Session start: {session_label} | Next: {L['nc']} | Sleep: {L['sleep']}s"
    )

    st.markdown("---")
    pnl_v = D['realized_pnl']
    total_resolved = D['wins'] + D['losses']
    trade_roi_pct = (pnl_v / D['bank_start'] * 100) if D['bank_start'] else None
    win_rate = (D['wins'] / total_resolved * 100) if total_resolved else None

    o1, o2, o3, o4, o5, o6 = st.columns(6)
    o1.metric("🏁 Стартовый банк", f"${D['bank_start']:.2f}")
    o2.metric("💼 Текущий банк", f"${bank:.2f}", delta=f"{bank_change:+.2f}")
    o3.metric("💰 Realized PnL", f"${pnl_v:+.2f}")
    o4.metric("📈 ROI", f"{trade_roi_pct:+.2f}%" if trade_roi_pct is not None else "—")
    o5.metric("🎯 Сделки", D['total_entered'])
    o6.metric("✅ Win rate", f"{win_rate:.0f}%" if win_rate is not None else "—")

    st.markdown("---")
    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Wins", D['wins'])
    g2.metric("Losses", D['losses'])
    g3.metric("Pending", D['pending'])
    g4.metric("Mode", mode)

    st.markdown("---")
    st.markdown("#### Live all-time")
    lt1, lt2, lt3, lt4 = st.columns(4)
    live_total_resolved = D['live_all_time_wins'] + D['live_all_time_losses']
    live_all_time_wr = (D['live_all_time_wins'] / live_total_resolved * 100) if live_total_resolved else None
    lt1.metric("Live trades", D['live_all_time_trades'])
    lt2.metric("Live PnL", f"${D['live_all_time_pnl']:+.2f}")
    lt3.metric("Live wins", D['live_all_time_wins'])
    lt4.metric("Live WR", f"{live_all_time_wr:.0f}%" if live_all_time_wr is not None else "—")

    st.markdown("---")
    st.markdown("#### Последние 5 сделок")
    recent_trade_rows = []
    for x in reversed(D['last_entered'][-5:]):
        realized = x.get("realized_pnl")
        status = "WIN" if x.get("won") is True else "LOSS" if x.get("won") is False else "PENDING"
        recent_trade_rows.append({
            "Time": x.get("timestamp", ""),
            "Coin": x.get("coin", ""),
            "Side": x.get("side", ""),
            "PnL": f"${float(realized or 0):+.2f}" if realized is not None else "—",
            "Status": status,
        })
    if recent_trade_rows:
        st.dataframe(recent_trade_rows, width="stretch", hide_index=True, height=220)
    else:
        st.caption("Сделок пока нет.")

    st.markdown("---")
    quick1, quick2, quick3, quick4 = st.columns(4)
    quick1.metric("BTC", f"${D['btc_price']:.0f}" if D['btc_price'] else "—")
    quick2.metric("ETH", f"${D['eth_price']:.0f}" if D['eth_price'] else "—")
    quick3.metric("Signals", D['total_signals'])
    quick4.metric("Binance", "❌ Err" if L['err'] else "✅ OK")

    with st.expander("Показать дополнительные runtime-метрики", expanded=False):
        extra1, extra2, extra3, extra4 = st.columns(4)
        extra1.metric("Session Bank Start", f"${float(L.get('session_bank_start') or D['bank_start']):.2f}")
        extra2.metric("Signals in Session", D['total_signals'])
        extra3.metric("Portfolio vs Start", f"${net_result:+.2f}" if net_result is not None else "—")
        extra4.metric("Updated", D['time'])

        if D['last_full_window_waits']:
            wait_rows = []
            for x in reversed(D['last_full_window_waits'][-5:]):
                wait_rows.append({
                    "Time": x.get('timestamp', ''),
                    "Coin": x.get('coin', ''),
                    "Core EV": x.get('core_ev_decision', ''),
                    "Time Left": f"{x.get('time_left', 0):.1f}s",
                    "Reason": str(x.get('full_window_entry_reason', x.get('reason', '')) or '')[:56],
                })
            st.markdown("**Последние ожидания full-window**")
            st.dataframe(wait_rows, width="stretch", hide_index=True, height=180)

# ==========================================
# TAB 4: STATISTICS
# ==========================================
with tab_stats:
    st.markdown("### 📈 Statistics")
    st.caption("Глубокая аналитика по Core EV, отказам и времени входа.")

    if D['total_signals'] > 0:
        stat_n = st.selectbox("Период Core EV runtime", [50, 100, 200], index=2)
        runtime_counts = D['core_ev_runtime_decisions_by_n'].get(stat_n, D['core_ev_runtime_decisions'])
        runtime_rows = D['core_ev_recent_rows_by_n'].get(stat_n, D['core_ev_recent_rows'])
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Signals", D['total_signals'])
        s2.metric("Entries", D['total_entered'])
        s3.metric("Resolved Windows", D['resolved_window_samples'])
        s4.metric("Rulebook Buckets", D['core_ev_rulebook_bucket_count'])

        st.markdown("---")

        st.markdown(f"**Core EV решения за последние {stat_n} сигналов**")
        rt1, rt2, rt3, rt4 = st.columns(4)
        rt1.metric("ALLOW", runtime_counts.get('allow', 0))
        rt2.metric("STRONG_ALLOW", runtime_counts.get('strong_allow', 0))
        rt3.metric("MICRO_ALLOW", runtime_counts.get('micro_allow', 0))
        rt4.metric("DENY", runtime_counts.get('deny', 0))

        if runtime_rows:
            st.dataframe(runtime_rows, width="stretch", hide_index=True, height=240)

        st.markdown("---")

        st.markdown("**Completed Trades for Manual Review**")
        st.caption("Минимальный набор по завершённым сделкам: время входа/закрытия, решение, уровень, PM, delta, time left, size, PnL, outcome и exec status.")
        if D['settled_trade_rows']:
            st.dataframe(D['settled_trade_rows'], width="stretch", hide_index=True, height=320)
        else:
            st.caption("Пока нет завершённых сделок для таблицы разбора.")

        st.markdown("---")

        if D['win_rate_by_time_bucket']:
            st.markdown("**Win Rate by Time Left Bucket**")
            wr_rows = []
            for key, payload in D['win_rate_by_time_bucket'].items():
                wins = int(payload.get("wins", 0) or 0)
                losses = int(payload.get("losses", 0) or 0)
                total = wins + losses
                wr_rows.append({
                    "Time Bucket": key,
                    "Wins": wins,
                    "Losses": losses,
                    "Win Rate": f"{(wins / total * 100):.0f}%" if total else "—",
                })
            st.dataframe(wr_rows, width="stretch", hide_index=True, height=220)

        st.markdown("---")
        st.markdown("**Core EV Denial Reasons Breakdown**")
        if D['core_ev_deny_reasons']:
            total_core_ev_denies = sum(D['core_ev_deny_reasons'].values())
            for rr, cnt in sorted(D['core_ev_deny_reasons'].items(), key=lambda x: -x[1]):
                pct = cnt / max(total_core_ev_denies, 1)
                st.progress(pct, text=f"{rr}: {cnt} ({pct*100:.0f}%)")
        else:
            st.caption("No Core EV deny data")

        st.markdown("---")
        r1, r2 = st.columns(2)
        with r1:
            st.markdown("**Top Allow Buckets**")
            if D['top_allow_buckets']:
                allow_rows = []
                for row in D['top_allow_buckets'][:10]:
                    allow_rows.append({
                        "Decision": row.get("decision", ""),
                        "Trades": row.get("trades", 0),
                        "ROI": f"{float(row.get('roi', 0) or 0):+.1f}%",
                        "Bucket": str(row.get("key", ""))[:80],
                    })
                st.dataframe(allow_rows, width="stretch", hide_index=True, height=260)
            else:
                st.caption("No allow buckets yet.")

        with r2:
            st.markdown("**Top Deny Buckets**")
            if D['top_deny_buckets']:
                deny_rows = []
                for row in D['top_deny_buckets'][:10]:
                    deny_rows.append({
                        "Decision": row.get("decision", ""),
                        "Trades": row.get("trades", 0),
                        "ROI": f"{float(row.get('roi', 0) or 0):+.1f}%",
                        "Bucket": str(row.get("key", ""))[:80],
                    })
                st.dataframe(deny_rows, width="stretch", hide_index=True, height=260)
            else:
                st.caption("No deny buckets yet.")

        st.caption("Statistics reset is disabled in the dashboard to protect live runtime data.")
    else:
        st.info("No statistics yet. Statistics are built from saved signals.")

    st.markdown("---")
    log_header_cols = st.columns([3, 1])
    with log_header_cols[0]:
        st.markdown("### Последние события (лог)")
    with log_header_cols[1]:
        if st.button("🔄 Refresh log", width="stretch"):
            load_log_tail.clear()
            st.rerun()

    log_controls = st.columns([1.2, 1.2, 1.6])
    with log_controls[0]:
        log_rows = st.selectbox("Строк лога", [100, 150, 200], index=1, key="stats_log_rows")
    with log_controls[1]:
        log_only_important = st.checkbox("Только важные", value=False, key="stats_log_only_important")
    with log_controls[2]:
        log_highlight = st.checkbox("Подсвечивать ключевые события", value=True, key="stats_log_highlight")

    log_path = _resolve_stats_log_path(L, settings)
    raw_log_lines = load_log_tail(str(log_path), log_rows)
    visible_log_lines = raw_log_lines
    if log_only_important:
        visible_log_lines = [line for line in raw_log_lines if _classify_log_line(line)[1]]

    st.caption(
        f"Источник: {L.get('service_name') or 'Unknown'} | Файл: {log_path} | Показано строк: {len(visible_log_lines)}/{len(raw_log_lines)}"
    )
    render_log_lines(visible_log_lines, highlight=log_highlight)

    with st.expander("Показать raw log", expanded=False):
        if raw_log_lines:
            st.code("\n".join(raw_log_lines), language=None)
        else:
            st.caption("Лог пока пуст или ещё не создан.")

# ==========================================
# TAB 4: SETTINGS
# ==========================================
with tab_settings:
    st.markdown("### ⚙️ Settings")
    st.markdown("<div class='safe-box'><strong>Основные настройки сверху.</strong> Тонкая калибровка спрятана в расширенный блок.</div>", unsafe_allow_html=True)
    st.warning("Сохранение и перезапуск доступны только после ввода пароля, если он включён.")

    settings_password = os.getenv("DASHBOARD_PASSWORD", os.getenv("DASHBOARD_SETTINGS_PASSWORD", ""))
    unlock_until = float(st.session_state.get("settings_unlock_until", 0.0) or 0.0)
    settings_unlocked = (not settings_password) or unlock_until > time.time()

    # ===== MANUAL SETTINGS =====
    new_settings = settings.copy()

    status1, status2, status3 = st.columns(3)
    status1.metric("Runtime mode", str(L.get("mode") or "Unknown"))
    status2.metric("Desired mode", str(settings.get("desired_mode") or "dry-run"))
    status3.metric("Bot status", "active" if bot_running else "inactive")

    st.markdown("---")

    basic1, basic2 = st.columns(2)
    with basic1:
        new_settings["desired_mode"] = st.selectbox("Режим для запуска", ["dry-run", "live"], index=["dry-run", "live"].index(str(settings.get("desired_mode", "dry-run") or "dry-run")))
        new_settings["amount"] = st.number_input("Размер ставки (USDC)", min_value=1.0, max_value=1000.0, value=float(settings.get("amount", 10)), step=1.0)
    with basic2:
        new_settings["sim_bank"] = st.number_input("Симулированный банк dry-run (USDC)", min_value=10.0, max_value=100000.0, value=float(settings.get("sim_bank", settings.get("bank", 100))), step=10.0)
        new_settings["bank"] = float(new_settings["sim_bank"])
        new_settings["enabled_coins"] = st.multiselect("Активные монеты", ["BTC", "ETH"], default=settings.get("enabled_coins", ["BTC", "ETH"]))

    with st.expander("Расширенные настройки: Core EV / full-window / L1 fallback / risk", expanded=False):
        s1, s2, s3 = st.columns(3)
        with s1:
            st.markdown("**Bank / Sizing**")
            new_settings["daily_loss_limit"] = st.number_input("Дневной стоп-лосс (USDC)", min_value=1.0, max_value=1000.0, value=float(settings.get("daily_loss_limit", 15.0)), step=1.0)
            new_settings["daily_loss_limit_pct"] = st.slider("Дневной стоп-лосс (% от банка)", min_value=0.0, max_value=0.50, value=float(settings.get("daily_loss_limit_pct", 0.0)), step=0.05, format="%.2f")
            new_settings["dynamic_sizing"] = st.checkbox("Динамический размер ставки", value=bool(settings.get("dynamic_sizing", True)))
            new_settings["dynamic_min_amount"] = st.number_input("Мин ставка (USDC)", min_value=1.0, max_value=1000.0, value=float(settings.get("dynamic_min_amount", 5.0)), step=1.0)
            new_settings["dynamic_max_amount"] = st.number_input("Макс ставка (USDC)", min_value=1.0, max_value=1000.0, value=float(settings.get("dynamic_max_amount", 15.0)), step=1.0)
            new_settings["dynamic_base_risk_pct"] = st.slider("Базовый риск", min_value=0.01, max_value=0.10, value=float(settings.get("dynamic_base_risk_pct", 0.05)), step=0.01, format="%.2f")
            new_settings["dynamic_step_bank_gain_pct"] = st.slider("Шаг роста банка", min_value=0.10, max_value=2.0, value=float(settings.get("dynamic_step_bank_gain_pct", 0.70)), step=0.05, format="%.2f")
            new_settings["dynamic_step_risk_pct"] = st.slider("Прирост риска за шаг", min_value=0.0, max_value=0.05, value=float(settings.get("dynamic_step_risk_pct", 0.01)), step=0.01, format="%.2f")
            new_settings["dynamic_max_risk_pct"] = st.slider("Макс риск", min_value=0.01, max_value=0.15, value=float(settings.get("dynamic_max_risk_pct", 0.08)), step=0.01, format="%.2f")

        with s2:
            st.markdown("**Core EV / Full-Window**")
            new_settings["core_ev_enabled"] = st.checkbox("Core EV enabled", value=bool(settings.get("core_ev_enabled", True)))
            new_settings["core_ev_pm_min"] = st.number_input("Base PM min", min_value=0.01, max_value=1.0, value=float(settings.get("core_ev_pm_min", 0.58)), step=0.01, format="%.2f")
            new_settings["core_ev_pm_max"] = st.number_input("Base PM max", min_value=0.01, max_value=1.0, value=float(settings.get("core_ev_pm_max", 0.70)), step=0.01, format="%.2f")
            new_settings["core_ev_flex_pm_min"] = st.number_input("Flex PM min", min_value=0.01, max_value=1.0, value=float(settings.get("core_ev_flex_pm_min", 0.50)), step=0.01, format="%.2f")
            new_settings["core_ev_flex_pm_max"] = st.number_input("Flex PM max", min_value=0.01, max_value=1.0, value=float(settings.get("core_ev_flex_pm_max", 0.99)), step=0.01, format="%.2f")
            new_settings["core_ev_entry_time_min"] = st.number_input("Core EV entry time min (s)", min_value=1, max_value=300, value=int(settings.get("core_ev_entry_time_min", 10)), step=1)
            new_settings["core_ev_entry_time_max"] = st.number_input("Core EV entry time max (s)", min_value=10, max_value=305, value=int(settings.get("core_ev_entry_time_max", 305)), step=5)
            new_settings["core_ev_time_left_min"] = st.number_input("Core EV active time-left min (s)", min_value=1, max_value=300, value=int(settings.get("core_ev_time_left_min", settings.get("core_ev_entry_time_min", 10))), step=1)
            new_settings["core_ev_time_left_max"] = st.number_input("Core EV active time-left max (s)", min_value=1, max_value=305, value=int(settings.get("core_ev_time_left_max", 20)), step=1)
            new_settings["full_window_core_ev_enabled"] = st.checkbox("Full-window Core EV enabled", value=bool(settings.get("full_window_core_ev_enabled", True)))
            new_settings["full_window_core_ev_time_left_max"] = st.number_input("Full-window eval max time left (s)", min_value=10, max_value=300, value=int(settings.get("full_window_core_ev_time_left_max", 180)), step=5)
            new_settings["full_window_core_ev_min_level"] = st.selectbox("Min Core EV bucket level", ["L1", "L2", "L3"], index=["L1", "L2", "L3"].index(str(settings.get("full_window_core_ev_min_level", "L2"))))
            new_settings["full_window_entry_confirm_ticks"] = st.number_input("Confirm ticks", min_value=1, max_value=10, value=int(settings.get("full_window_entry_confirm_ticks", 2)), step=1)
            new_settings["full_window_entry_commit_time_left"] = st.number_input("Commit time left (s)", min_value=1, max_value=120, value=int(settings.get("full_window_entry_commit_time_left", 19)), step=1)
            new_settings["full_window_entry_min_score_gain"] = st.number_input("Min score gain to keep waiting", min_value=0.0, max_value=5.0, value=float(settings.get("full_window_entry_min_score_gain", 0.15)), step=0.05, format="%.2f")
            new_settings["full_window_micro_entry_commit_time_left"] = st.number_input("Micro commit time left (s)", min_value=1, max_value=120, value=int(settings.get("full_window_micro_entry_commit_time_left", 30)), step=1)

        with s3:
            st.markdown("**L1 Fallback / Risk / Trend Conflict**")
            new_settings["full_window_l1_fallback_min_trades"] = st.number_input("L1 fallback min trades", min_value=1, max_value=100, value=int(settings.get("full_window_l1_fallback_min_trades", 8)), step=1)
            new_settings["full_window_l1_fallback_require_recent_positive"] = st.checkbox("L1 fallback require recent positive", value=bool(settings.get("full_window_l1_fallback_require_recent_positive", True)))
            new_settings["full_window_l1_fallback_time_left_max"] = st.number_input("L1 fallback max time left (s)", min_value=10, max_value=300, value=int(settings.get("full_window_l1_fallback_time_left_max", 150)), step=5)
            new_settings["full_window_l1_strong_exception_min_trades"] = st.number_input("L1 strong exception min trades", min_value=1, max_value=100, value=int(settings.get("full_window_l1_strong_exception_min_trades", 2)), step=1)
            new_settings["full_window_l1_strong_exception_min_roi"] = st.number_input("L1 strong exception min ROI %", min_value=-100.0, max_value=500.0, value=float(settings.get("full_window_l1_strong_exception_min_roi", 50.0)), step=1.0, format="%.1f")
            new_settings["core_ev_max_risk_pct"] = st.slider("Core EV max risk %", min_value=0.001, max_value=0.05, value=float(settings.get("core_ev_max_risk_pct", 0.02)), step=0.001, format="%.3f")
            new_settings["core_ev_micro_risk_pct"] = st.slider("Core EV micro risk %", min_value=0.001, max_value=0.02, value=float(settings.get("core_ev_micro_risk_pct", 0.005)), step=0.001, format="%.3f")
            new_settings["core_ev_trend_conflict_micro_delta_min_pct"] = st.number_input("Trend-conflict micro delta min %", min_value=0.0, max_value=0.2, value=float(settings.get("core_ev_trend_conflict_micro_delta_min_pct", 0.012)), step=0.001, format="%.3f")
            new_settings["core_ev_trend_conflict_micro_confidence_min"] = st.number_input("Trend-conflict micro confidence min", min_value=0.0, max_value=1.0, value=float(settings.get("core_ev_trend_conflict_micro_confidence_min", 0.0)), step=0.01, format="%.2f")
            new_settings["core_ev_trend_conflict_micro_indicator_min"] = st.number_input("Trend-conflict micro indicator min", min_value=-1.0, max_value=1.0, value=float(settings.get("core_ev_trend_conflict_micro_indicator_min", -0.10)), step=0.01, format="%.2f")
            new_settings["trend_conflict_override_delta_min_pct"] = st.number_input("Trend conflict override delta min %", min_value=0.0, max_value=0.2, value=float(settings.get("trend_conflict_override_delta_min_pct", 0.025)), step=0.001, format="%.3f")
            new_settings["shadow_live_mode"] = st.selectbox("Shadow live mode", ["observe", "off", "block_deny", "hybrid"], index=["observe", "off", "block_deny", "hybrid"].index(str(settings.get("shadow_live_mode", "observe"))))
            new_settings["window_sample_logging_enabled"] = st.checkbox("Window sample logging enabled", value=bool(settings.get("window_sample_logging_enabled", True)))

    # ===== BUTTONS =====
    st.markdown("---")
    if settings_password:
        st.markdown("**Защита настроек**")
        entered_password = st.text_input(
            "Пароль для управления настройками и сервисами",
            type="password",
            key="settings_password_input",
        )
        if st.button("🔓 Разблокировать", width="stretch"):
            if entered_password == settings_password:
                st.session_state["settings_unlock_until"] = time.time() + 600
                settings_unlocked = True
                st.success("Доступ открыт на 10 минут")
            else:
                st.error("Неверный пароль")
        elif settings_unlocked:
            unlock_left = max(0, int(unlock_until - time.time()))
            st.success(f"Доступ открыт, осталось {unlock_left}s")
        else:
            st.info("Введите пароль и нажмите Разблокировать")

    action_cols = st.columns([1, 1, 1])

    with action_cols[0]:
        if st.button("▶️ Запустить бота", width="stretch"):
            if not settings_unlocked:
                st.error("Сначала разблокируйте управление")
            else:
                desired_mode = str(new_settings.get("desired_mode", "dry-run") or "dry-run")
                target_service = "poly-bot-live.service" if desired_mode == "live" else "poly-bot-test.service"
                ok, detail = start_bot_for_mode(desired_mode)
                if ok:
                    runtime_after_start = wait_for_service_active(target_service)
                    account_seed = fetch_polymarket_account_state() if desired_mode == "live" else {}
                    create_new_session(new_settings, runtime_after_start, account_seed)
                    st.success("Бот запущен")
                else:
                    st.error(detail)

    with action_cols[1]:
        if st.button("⏹️ Остановить бота", width="stretch"):
            if not settings_unlocked:
                st.error("Сначала разблокируйте управление")
            else:
                ok, detail = stop_all_bot_services()
                if ok:
                    st.success("Бот остановлен")
                else:
                    st.error(detail)

    with action_cols[2]:
        if st.button("🧹 Сбросить live статистику", width="stretch"):
            if not settings_unlocked:
                st.error("Сначала разблокируйте управление")
            else:
                stats = load_stats_state()
                stats["live_stats_reset_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                save_stats_state(stats)
                st.success("Live статистика сброшена через reset marker")

    session_cols = st.columns([1, 1])
    with session_cols[0]:
        if st.button("🆕 Новая сессия", width="stretch"):
            create_new_session(new_settings, L, fetch_polymarket_account_state() if str(L.get("mode") or "Unknown") == "live" else {})
            st.success("Создана новая сессия")
            st.rerun()
    with session_cols[1]:
        st.caption(f"Session ID: {session_state.get('session_id') or 'Unknown'}")

    btn_cols = st.columns([1, 1, 1])

    with btn_cols[0]:
        if st.button("💾 Сохранить", type="primary", width="stretch"):
            if not settings_unlocked:
                st.error("Введите правильный пароль для сохранения настроек")
            else:
                new_settings["first_setup_done"] = True
                save_settings(new_settings)
                st.success("✅ Настройки сохранены. Hot-reloadable параметры бот подхватит без рестарта, режим меняется только через systemd actions.")
                st.rerun()

    with btn_cols[1]:
        if st.button("🔄 Сбросить к дефолтным", width="stretch"):
            if not settings_unlocked:
                st.error("Введите правильный пароль для сброса настроек")
            else:
                defaults = get_default_settings()
                defaults["first_setup_done"] = True
                save_settings(defaults)
                st.success("✅ Восстановлены безопасные дефолтные настройки текущей стратегии.")
                st.rerun()

    with btn_cols[2]:
        if st.button("🔁 Restart active service", width="stretch"):
            if not settings_unlocked:
                st.error("Введите правильный пароль для перезапуска бота")
            else:
                active_service = str(L.get("service_name") or "poly-bot-live.service")
                ok, detail = restart_bot_service(active_service)
                if ok:
                    st.success(f"✅ {detail}")
                else:
                    st.error(f"Restart failed: {detail}")

    st.caption("Настройки сохраняются в `settings.json` атомарно. Для применения нужен перезапуск systemd service.")
    if not settings_password:
        st.caption("Защита настроек не включена. Чтобы включить пароль, задайте переменную `DASHBOARD_PASSWORD` в systemd service.")
