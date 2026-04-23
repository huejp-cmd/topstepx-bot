"""
topstepx_bot.py — Railway version (sans Mac requis)
Reçoit les alertes TradingView → appelle l'API TopstepX directement
"""
import json, logging, os, threading, time, requests
from datetime import datetime
from flask import Flask, request, jsonify
from zoneinfo import ZoneInfo

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("TSX-Bot")

WEBHOOK_TOKEN  = os.environ.get("WEBHOOK_TOKEN", "jp_tsx_mnq_2026")
ACCOUNT_ID     = int(os.environ.get("ACCOUNT_ID", "22024523"))
CONTRACT_ID    = os.environ.get("CONTRACT_ID", "CON.F.US.MNQ.M26")
UNIT_VALUE_USD = float(os.environ.get("UNIT_VALUE_USD", "50"))
TSX_API        = "https://api.topstepx.com"
TZ_ET          = ZoneInfo("America/New_York")

# Token géré en mémoire + env var
_token = os.environ.get("TOPSTEPX_TOKEN", "")

def get_token():
    return _token

def tsx_post(path: str, body: dict) -> dict:
    tok = get_token()
    if not tok:
        return {"error": "no_token", "success": False}
    try:
        r = requests.post(f"{TSX_API}{path}",
                          headers={"Authorization": f"Bearer {tok}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e), "success": False}

def place_order(side: str, qty: int) -> dict:
    return tsx_post("/api/Order/place", {
        "accountId": ACCOUNT_ID, "contractId": CONTRACT_ID,
        "side": 0 if side.upper() in ("BUY","LONG") else 1,
        "type": 2, "size": qty,
        "limitPrice": None, "stopPrice": None, "isClientId": False
    })

def close_all() -> dict:
    return tsx_post("/api/Position/closeContract",
                    {"accountId": ACCOUNT_ID, "contractId": CONTRACT_ID})

def get_positions() -> list:
    r = tsx_post("/api/Position/searchOpen", {"accountId": ACCOUNT_ID})
    return r.get("positions", []) if isinstance(r, dict) else []

def get_account() -> dict:
    r = tsx_post("/api/Account/search", {"onlyActiveAccounts": False})
    accounts = r.get("accounts", []) if isinstance(r, dict) else []
    for a in accounts:
        if a.get("id") == ACCOUNT_ID:
            return a
    return {}

# ── Labouchere ─────────────────────────────────────────────────────────────────
class LabTracker:
    INIT_SEQ = [1, 1, 1, 1]
    def __init__(self, unit_value=UNIT_VALUE_USD):
        self.unit_value = unit_value
        self.seq   = list(self.INIT_SEQ)
        self.tape  = [{"val": v, "status": "active", "cycle": 0} for v in self.INIT_SEQ]
        self.cycle = 0
        self.trades = []
        self.wins = self.losses = 0
        self._lock = threading.Lock()

    def _bet(self):
        if not self.seq: return 2
        if len(self.seq) == 1: return self.seq[0]*2
        return self.seq[0] + self.seq[-1]

    def record(self, side, contracts, result, pnl=0.0):
        with self._lock:
            bet = self._bet()
            self.trades.append({"time": datetime.now(TZ_ET).strftime("%H:%M"),
                                 "side": side, "contracts": contracts,
                                 "bet_usd": round(bet*self.unit_value,2),
                                 "result": result, "pnl": round(pnl,2)})
            if result == "win":
                self.wins += 1
                add = bet if bet < 6 else max(1, round(bet/3)*2)
                self.seq.append(add)
                self.tape.append({"val": add, "status": "active", "cycle": self.cycle})
            else:
                self.losses += 1
                act = [i for i,e in enumerate(self.tape)
                       if e["status"]=="active" and e["cycle"]==self.cycle]
                if len(act) >= 2:
                    self.tape[act[0]]["status"] = self.tape[act[-1]]["status"] = "crossed"
                    self.seq = self.seq[1:-1]
                elif len(act) == 1:
                    self.tape[act[0]]["status"] = "crossed"; self.seq = []
                if len(self.seq) < 2:
                    self.cycle += 1
                    self.seq = list(self.INIT_SEQ)
                    self.tape.append({"val": None, "status": "separator", "cycle": self.cycle})
                    for v in self.INIT_SEQ:
                        self.tape.append({"val": v, "status": "active", "cycle": self.cycle})

    def state(self):
        with self._lock:
            bet = self._bet()
            return {"seq": list(self.seq), "bet_units": bet,
                    "bet_usd": round(bet*self.unit_value,2),
                    "wins": self.wins, "losses": self.losses,
                    "cycle": self.cycle, "total_trades": len(self.trades)}

    def tape_html(self):
        with self._lock:
            bet = self._bet()
            parts = []
            for e in self.tape:
                if e["status"] == "separator":
                    parts.append(f'<span style="color:#555;margin:0 8px">│</span><span style="color:#aaa;font-size:11px">Cycle {e["cycle"]+1}</span>')
                    continue
                usd = round(e["val"]*self.unit_value,2) if e["val"] else 0
                label = f'${usd:.0f}'
                if e["status"] == "crossed":
                    parts.append(f'<span style="text-decoration:line-through;color:#ff4444;margin:2px 4px">{label}</span>')
                else:
                    act = [x for x in self.tape if x["status"]=="active" and x["cycle"]==e["cycle"]]
                    hl = act and (act[0] is e or act[-1] is e)
                    c = "#00e676" if hl else "#c9d1d9"
                    b = "border:1px solid #00e676;border-radius:3px;padding:1px 4px;" if hl else ""
                    parts.append(f'<span style="color:{c};margin:2px 4px;{b}">{label}</span>')
            rows = "".join(f'<tr><td style="color:#888;padding:3px 8px">{t["time"]}</td><td style="padding:3px 8px">{t["side"].upper()}</td><td style="padding:3px 8px">{t["contracts"]} MNQ</td><td style="padding:3px 8px">{t["bet_usd"]:.0f}$</td><td style="color:{"#00e676" if t["result"]=="win" else "#ff4444"};font-weight:bold;padding:3px 8px">{t["result"].upper()}</td><td style="color:{"#00e676" if t["result"]=="win" else "#ff4444"};padding:3px 8px">{"+" if t["result"]=="win" else "-"}{abs(t["pnl"]):.0f}$</td></tr>' for t in reversed(self.trades[-10:]))
            return ("".join(parts) +
                    f'<div style="margin-top:10px;padding:8px;background:#1a1f2e;border-radius:6px;border-left:3px solid #00e676"><b style="color:#00e676">Prochaine mise :</b> <span style="color:#fff;font-size:16px">${bet*self.unit_value:.0f}</span> <span style="color:#888">({bet} unités × ${self.unit_value:.0f})</span></div>' +
                    (f'<table style="font-family:monospace;font-size:13px;width:100%;margin-top:16px"><tr style="color:#555"><th>Heure</th><th>Côté</th><th>Contrats</th><th>Mise</th><th>Résultat</th><th>P&L</th></tr>{rows}</table>' if rows else ""))

lab = LabTracker()
_current_trade = {}

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat(),
                    "token_ok": bool(_token)})

@app.route('/set-token', methods=['POST'])
def set_token():
    global _token
    body = request.get_json(force=True, silent=True) or {}
    t = body.get('token', '')
    if not t:
        return jsonify({'error': 'no token'}), 400
    _token = t
    return jsonify({'status': 'ok', 'token_prefix': t[:20]})

@app.route('/account')
def account():
    return jsonify(get_account())

@app.route('/positions')
def positions():
    return jsonify({'positions': get_positions()})

@app.route('/webhook', methods=['POST'])
def webhook():
    global _current_trade
    tok = request.headers.get('X-Webhook-Token') or request.args.get('token','')
    if tok != WEBHOOK_TOKEN:
        return jsonify({'error': 'unauthorized'}), 401
    data      = request.get_json(force=True, silent=True) or {}
    action    = data.get('action','').lower()
    contracts = int(data.get('contracts', lab.state()['bet_units']))
    lab_result= data.get('lab_result','')
    lab_pnl   = float(data.get('lab_pnl', 0) or 0)
    log.info(f"Signal: action={action} contracts={contracts}")
    now_et   = datetime.now(TZ_ET)
    bar_time = now_et.hour*100 + now_et.minute
    if bar_time < 930 or bar_time >= 1545:
        return jsonify({'status': 'ignored', 'reason': 'outside_session'})
    if action in ('buy','long'):
        if lab_result in ('win','loss') and _current_trade:
            lab.record(_current_trade.get('side','?'), _current_trade.get('contracts',contracts), lab_result, lab_pnl)
        _current_trade = {'side':'long','contracts':contracts}
        result = place_order('BUY', contracts)
        log.info(f"BUY {contracts} → {result}")
    elif action in ('sell','short'):
        if lab_result in ('win','loss') and _current_trade:
            lab.record(_current_trade.get('side','?'), _current_trade.get('contracts',contracts), lab_result, lab_pnl)
        _current_trade = {'side':'short','contracts':contracts}
        result = place_order('SELL', contracts)
        log.info(f"SELL {contracts} → {result}")
    elif action == 'close':
        result = close_all()
        log.info(f"CLOSE → {result}")
    else:
        return jsonify({'error': f'unknown action: {action}'}), 400
    return jsonify({'status':'ok','action':action,'contracts':contracts,'result':result,'lab':lab.state()})

@app.route('/lab/win', methods=['POST'])
def lab_win():
    body = request.get_json(force=True, silent=True) or {}
    pnl  = float(body.get('pnl',0) or 0)
    side = _current_trade.get('side','long') if _current_trade else 'long'
    ctr  = _current_trade.get('contracts',2) if _current_trade else 2
    lab.record(side, ctr, 'win', pnl)
    return jsonify({'status':'ok','lab':lab.state()})

@app.route('/lab/loss', methods=['POST'])
def lab_loss():
    body = request.get_json(force=True, silent=True) or {}
    pnl  = float(body.get('pnl',0) or 0)
    side = _current_trade.get('side','long') if _current_trade else 'long'
    ctr  = _current_trade.get('contracts',2) if _current_trade else 2
    lab.record(side, ctr, 'loss', pnl)
    return jsonify({'status':'ok','lab':lab.state()})

@app.route('/lab/state')
def lab_state():
    return jsonify(lab.state())

@app.route('/dashboard')
def dashboard():
    pos  = get_positions()
    acct = get_account()
    ls   = lab.state()
    now  = datetime.now(TZ_ET).strftime('%H:%M:%S ET')
    bal  = acct.get('balance', 0)
    total = ls['total_trades']
    wr = f"{ls['wins']/total*100:.0f}%" if total else "—"
    pos_html = "".join(f'<div style="padding:6px 10px;background:#161b22;border-radius:4px;margin:4px 0"><span style="color:{"#00e676" if p.get("type")==0 else "#ff4444"};font-weight:bold">{"LONG" if p.get("type")==0 else "SHORT"}</span> {p.get("size","?")} MNQ @ {p.get("averagePrice","?")}</div>' for p in pos) or '<p style="color:#555">Aucune position ouverte</p>'
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>TopstepX Dashboard</title>
<meta http-equiv="refresh" content="30">
<style>body{{font-family:'Courier New',monospace;background:#0d1117;color:#c9d1d9;padding:20px;max-width:900px;margin:auto}}h2{{color:#fff;border-bottom:1px solid #30363d;padding-bottom:8px}}h3{{color:#888;margin-top:24px}}.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin:10px 0}}.stat{{display:inline-block;margin:0 16px;text-align:center}}.stat .val{{font-size:22px;font-weight:bold;color:#fff}}.stat .lbl{{font-size:11px;color:#888}}.green{{color:#00e676}}.red{{color:#ff4444}}</style></head>
<body>
<h2>🚀 TopstepX Bot — Dashboard <span style="font-size:13px;color:#555">{now}</span></h2>
<div class="card">
<div class="stat"><div class="val">${bal:,.0f}</div><div class="lbl">Solde</div></div>
<div class="stat"><div class="val {'green' if ls['wins']>ls['losses'] else 'red'}">{wr}</div><div class="lbl">Win Rate</div></div>
<div class="stat"><div class="val green">{ls['wins']}</div><div class="lbl">Wins</div></div>
<div class="stat"><div class="val red">{ls['losses']}</div><div class="lbl">Losses</div></div>
<div class="stat"><div class="val">{ls['cycle']+1}</div><div class="lbl">Cycle</div></div>
</div>
<h3>📊 Positions</h3>{pos_html}
<h3>🎯 Labouchere</h3>
<div class="card">
<div style="color:#888;font-size:12px;margin-bottom:8px">1 unité=${UNIT_VALUE_USD:.0f} · Séquence: [{', '.join(str(v) for v in ls['seq'])}]</div>
{lab.tape_html()}</div>
<p style="color:#333;font-size:11px;margin-top:24px">Railway Bot · Token valide · EOD force-close 16h00 ET</p>
</body></html>"""

# ── EOD Guardian ───────────────────────────────────────────────────────────────
_eod_closed_today = None
def eod_guardian():
    global _eod_closed_today
    while True:
        try:
            now_et = datetime.now(TZ_ET)
            today  = now_et.date()
            h, m   = now_et.hour, now_et.minute
            if h == 16 and m >= 0 and _eod_closed_today != today and now_et.weekday() < 5:
                pos = get_positions()
                if pos:
                    log.warning(f"EOD GUARDIAN — {len(pos)} positions → FORCE CLOSE")
                    close_all()
                _eod_closed_today = today
        except Exception as e:
            log.error(f"EOD Guardian: {e}")
        time.sleep(30)

threading.Thread(target=eod_guardian, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    log.info(f"TopstepX Railway Bot démarré sur port {port}")
    app.run(host='0.0.0.0', port=port)
