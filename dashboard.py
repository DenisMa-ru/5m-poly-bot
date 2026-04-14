"""
5m Poly Bot Dashboard — компактный, кнопка Refresh, таблица в expander.
Запуск: streamlit run dashboard.py --server.port 3001 --server.address 0.0.0.0 --server.headless true
"""
import streamlit as st
import re, os
from datetime import datetime
from pathlib import Path

DEFAULT_LOG = Path("/root/5m-poly-bot/bot.log")

st.set_page_config(page_title="5m Poly Bot", page_icon="📈", layout="wide",
                    initial_sidebar_state="collapsed")

# ===== HIDE STREAMLIT CHROME =====
st.markdown("""
<style>
    #MainMenu, footer, header { visibility: hidden; }
    [data-testid="stToolbar"] { display: none !important; }
    [data-testid="stSidebar"] { display: none; }
    [data-testid="stMetricLabel"] { font-size: 0.7em !important; color: #8b949e !important; text-transform: uppercase; }
    [data-testid="stMetricValue"] { font-size: 1.3em !important; }
    .block-container { padding-top: 0.5rem !important; padding-bottom: 0.3rem !important; }
    .skip-chip { display:inline-block; background:#2a1a1a; border-left:3px solid #ff6b6b;
                  padding:3px 8px; border-radius:3px; font-size:0.8em; color:#ff8888; margin:2px; }
    .trade-chip { display:inline-block; background:#1a2a1a; border-left:3px solid #51cf66;
                   padding:3px 8px; border-radius:3px; font-size:0.8em; color:#51cf66; margin:2px; }
    hr { margin: 6px 0 !important; border-color: #21262d !important; }
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
    if not line.strip(): return None
    r = {'raw': line}
    if _P["active"].search(line):
        r['type']='active'; m=_P["active"].search(line)
        if m: r['nc']=m.group(1)
        return r
    if _P["sleep"].search(line):
        r['type']='sleeping'; m=_P["sleep"].search(line)
        if m: r['sec']=int(m.group(1))
        m3=_P["snc"].search(line)
        if m3: r['nc']=m3.group(1)
        return r
    if _P["closed"].search(line): r['type']='closed'; return r
    if '🎯' in line and ('PM:Up@' in line or 'PM:Down@' in line):
        r['type']='signal'
        m=_P["coin"].search(line)
        if m: r['coin']=m.group(1)
        m=_P["time"].search(line)
        if m: r['t']=float(m.group(1))
        m=_P["pm"].search(line)
        if m: r['side']=m.group(1); r['pm']=float(m.group(2))
        m=_P["delta"].search(line)
        if m: r['d']=float(m.group(1))
        m=_P["conf"].search(line)
        if m: r['c']=int(m.group(1))
        m=_P["price"].search(line)
        if m: r['bp']=float(m.group(1).replace(',',''))
        m=_P["atr"].search(line)
        if m: r['atr']=float(m.group(2))
        return r
    if 'SKIP' in line:
        r['type']='skip'; m=_P["coin"].search(line)
        if m: r['coin']=m.group(1)
        raw=line
        if '< 0.94' in raw: r['r']='btc_low'
        elif '< 0.92' in raw: r['r']='eth_low'
        elif '> 0.99' in raw: r['r']='high'
        elif 'too close' in raw: r['r']='delta'
        elif 'confidence' in raw and '< 30%' in raw: r['r']='conf'
        elif 'ATR skip' in raw: r['r']='atr'
        else: r['r']='other'
        return r
    if re.compile(r'BINANCE ERROR').search(line): r['type']='err'; return r
    if _P["enter"].search(line):
        r['type']='trade'; m=_P["enter"].search(line)
        if m: r['coin']=m.group(1)
        m=_P["amt"].search(line)
        if m: r['amt']=float(m.group(1))
        m=_P["pnl"].search(line)
        if m: r['pnl']=float(m.group(1))
        return r
    return None

def parse_logs(path, n=3000):
    if not path or not path.exists(): return None, None
    try: lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except: return None, None
    evts = [e for l in lines[-n:] if (e := parse(l)) is not None]
    s = {'sig':0,'skip':0,'trade':0,'round':0,'err':0,'state':'start',
         'sleep':0,'nc':'--:--','btc_p':None,'eth_p':None,
         'invested':0,'pnl':0,'reasons':{},'last_sig':[],'last_skip':[],'last_trade':[],
         'time':datetime.now().strftime('%H:%M:%S'),'n':len(lines)}
    for e in evts:
        t=e['type']
        if t=='signal':
            s['sig']+=1; s['last_sig'].append(e)
            if e.get('coin')=='BTC' and e.get('bp'): s['btc_p']=e['bp']
            if e.get('coin')=='ETH' and e.get('bp'): s['eth_p']=e['bp']
        elif t=='skip':
            s['skip']+=1; s['last_skip'].append(e)
            rr=e.get('r','other'); s['reasons'][rr]=s['reasons'].get(rr,0)+1
        elif t=='trade':
            s['trade']+=1; s['last_trade'].append(e)
            s['invested']+=e.get('amt',0); s['pnl']+=e.get('pnl',0)
        elif t=='active': s['state']='active'; s['nc']=e.get('nc','--:--')
        elif t=='sleeping': s['state']='sleeping'; s['sleep']=e.get('sec',0); s['nc']=e.get('nc','--:--')
        elif t=='closed': s['round']+=1
        elif t=='err': s['err']+=1
    s['last_sig']=s['last_sig'][-12:]; s['last_skip']=s['last_skip'][-12:]
    return lines, s

RL = {'btc_low':'BTC<0.94','eth_low':'ETH<0.92','high':'>0.99',
      'delta':'δ<0.05%','conf':'conf<30%','atr':'ATR↑','other':'?'}

# ===== PARSE =====
log_path = Path(os.environ.get("BOT_LOG_FILE", str(DEFAULT_LOG)))
_, S = parse_logs(log_path)
if not S:
    st.error(f"Cannot read `{log_path}`"); st.stop()

# ===== ROW 1: Status bar =====
ico = {'sleeping':'💤','active':'⚡','start':'🚀'}.get(S['state'],'⚪')
st.markdown(f"### {ico} {S['state'].upper()} — Next: {S['nc']} | Sleep: {S['sleep']}s")

a1,a2,a3,a4,a5,a6 = st.columns(6)
a1.metric("Signals", S['sig'])
a2.metric("Rounds", S['round'])
a3.metric("Trades", S['trade'])
a4.metric("Binance", "❌Err" if S['err'] else "✅OK")
a5.metric("Skip Rate", f"{S['skip']/max(S['sig'],1)*100:.0f}%")
a6.metric("Updated", S['time'])

# ===== ROW 2: Money + Prices =====
b1,b2,b3,b4 = st.columns(4)
b1.metric("Invested", f"${S['invested']:.1f}")
pnl_v = S['pnl']
b2.metric("Expected PnL", f"${pnl_v:+.2f}",
           delta=f"{pnl_v:+.2f}",
           delta_color="normal" if pnl_v >= 0 else "inverse")
b3.metric("BTC", f"${S['btc_p']:.0f}" if S['btc_p'] else "—")
b4.metric("ETH", f"${S['eth_p']:.0f}" if S['eth_p'] else "—")

# ===== ROW 3: PM Prices + Skip Reasons =====
c1, c2 = st.columns(2)
with c1:
    st.markdown("**Polymarket**")
    for sig, name, mn in [
        (next((x for x in reversed(S['last_sig']) if x.get('coin')=='BTC'), None), "BTC", 0.94),
        (next((x for x in reversed(S['last_sig']) if x.get('coin')=='ETH'), None), "ETH", 0.92),
    ]:
        if sig and sig.get('pm'):
            p=sig['pm']; ok=mn <= p <= 0.99
            col="#51cf66" if ok else "#ff6b6b"
            lbl="✓" if ok else "✗"
            st.markdown(f"`{name}` <span style="color:{col};font-weight:bold">{p:.3f} {lbl}</span> "
                        f"δ{sig.get('d',0):.2f}% C{sig.get('c',0)}%", unsafe_allow_html=True)
        else:
            st.markdown(f"`{name}` —")
with c2:
    if S['reasons']:
        st.markdown("**Why skipping**")
        tot = S['skip']
        for rr, cnt in sorted(S['reasons'].items(), key=lambda x:-x[1]):
            pct = cnt/max(tot,1)
            st.progress(pct, text=f"{RL.get(rr,rr)}: {cnt} ({pct*100:.0f}%)")
    else:
        st.caption("No skips")

# ===== ROW 4: Trades + Skip chips =====
if S['last_trade'] or S['last_skip']:
    st.markdown("---")
    d1, d2 = st.columns(2)
    with d1:
        if S['last_trade']:
            st.markdown("**Trades**")
            chips = "".join(f'<span class="trade-chip">{x.get("coin","")} +${x.get("pnl",0):.2f}</span> '
                           for x in reversed(S['last_trade'][-8:]))
            st.markdown(chips, unsafe_allow_html=True)
    with d2:
        if S['last_skip']:
            st.markdown("**Last skips**")
            chips = "".join(f'<span class="skip-chip">{x.get("coin","")} {RL.get(x.get("r",""),"?")}</span> '
                           for x in reversed(S['last_skip'][-12:]))
            st.markdown(chips, unsafe_allow_html=True)

# ===== ROW 5: Signals table — COLLAPSED =====
if S['last_sig']:
    with st.expander(f"📋 Signals ({len(S['last_sig'])})", expanded=False):
        rows = []
        for x in reversed(S['last_sig']):
            rows.append({
                "Coin": x.get('coin',''), "Side": x.get('side',''),
                "PM": f"{x.get('pm',0):.3f}", "Δ%": f"{x.get('d',0):.3f}",
                "Conf%": x.get('c',0), "Time": f"{x.get('t',0):.0f}s",
            })
        st.dataframe(rows, use_container_width=True, hide_index=True, height=160)

# ===== FOOTER + REFRESH =====
st.markdown("---")
col_btn, col_info = st.columns([1, 5])
with col_btn:
    if st.button("🔄 Refresh", use_container_width=True):
        st.rerun()
with col_info:
    st.caption(f"5m Poly Bot | {S['time']} | {S['n']} lines")
