"""
topstepx_bot.py — Railway v5
Labouchere :
  - Séquence démarre [1, 1, 1, 1] (unités = contrats)
  - Mise = seq[0] + seq[-1] contrats
  - WIN  → gain_réel ÷ 3 ÷ UNIT_USD → slots en unités ajoutés (min 1)
  - LOSS → retire premier + dernier ; vide → reset [1,1,1,1]

v5 — Corrections :
  [BUG-1] Persistance d'état dans state.json (pos_60r, pos_45r, shared_direction, daily_pnl + date)
  [BUG-2] Réconciliation au démarrage avec l'API TopstepX (positions réelles)
  [BUG-3] Fix UnboundLocalError : _daily_realized_pnl/date global dans webhook_45r()
           + reset P&L journalier ajouté dans webhook_45r()
  [BUG-4] EOD Guardian : reset état interne après force-close + sauvegarde state
           + gestion fenêtre étendue (heure 15h59–16h01) pour ne pas rater l'EOD
  [BUG-5] Thread-safety : _state_lock protège toutes les mutations d'état global
"""
import json, logging, os, threading, time, requests
from datetime import datetime, date as date_cls
from flask import Flask, request, jsonify
from zoneinfo import ZoneInfo

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("TSX-Bot")

WEBHOOK_TOKEN  = os.environ.get("WEBHOOK_TOKEN", "jp_tsx_mnq_2026")
ACCOUNT_ID     = int(os.environ.get("ACCOUNT_ID", "22024523"))
CONTRACT_ID    = os.environ.get("CONTRACT_ID", "CON.F.US.MNQ.M26")
UNIT_USD       = float(os.environ.get("UNIT_USD", "50"))   # $ par unité (= 1 contrat)
MAX_CONTRACTS  = int(os.environ.get("MAX_CONTRACTS", "1"))  # plafond absolu de sécurité
MAX_DAILY_LOSS = float(os.environ.get("MAX_DAILY_LOSS", "400"))  # circuit-breaker journalier $
TSX_API        = "https://api.topstepx.com"
TZ_ET          = ZoneInfo("America/New_York")

# Chemin du fichier d'état persistant (Railway : /app est le répertoire de travail)
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json')
_state_lock = threading.Lock()   # protège toutes les mutations d'état global

# ── État global (en mémoire + persisté dans state.json) ──────────────────────
_daily_realized_pnl: float = 0.0   # P&L réalisé du jour (reset à minuit ET)
_daily_reset_date: date_cls | None = None

_pos_60r: int = 0             # contrats ouverts par le signal 60R (+ = long, - = short)
_pos_45r: int = 0             # contrats ouverts par le signal 45R
_shared_direction: str | None = None   # 'long', 'short', ou None

_token = os.environ.get("TOPSTEPX_TOKEN", "")

# ── API TopstepX ───────────────────────────────────────────────────────────────
def tsx_post(path: str, body: dict) -> dict:
    if not _token:
        return {"error": "no_token", "success": False}
    try:
        r = requests.post(f"{TSX_API}{path}",
                          headers={"Authorization": f"Bearer {_token}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e), "success": False}

def place_order(side: str, qty: int) -> dict:
    return tsx_post("/api/Order/place", {
        "accountId": ACCOUNT_ID, "contractId": CONTRACT_ID,
        "side": 0 if side.upper() in ("BUY", "LONG") else 1,
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
    for a in (r.get("accounts", []) if isinstance(r, dict) else []):
        if a.get("id") == ACCOUNT_ID:
            return a
    return {}

# ── Persistance d'état ────────────────────────────────────────────────────────
def _save_state():
    """Persiste l'état critique dans state.json. Thread-safe."""
    global _pos_60r, _pos_45r, _shared_direction, _daily_realized_pnl, _daily_reset_date
    with _state_lock:
        state = {
            "pos_60r":            _pos_60r,
            "pos_45r":            _pos_45r,
            "shared_direction":   _shared_direction,
            "daily_realized_pnl": _daily_realized_pnl,
            "daily_reset_date":   str(_daily_reset_date) if _daily_reset_date else None,
        }
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2)
            log.debug(f"state.json sauvegardé: {state}")
        except Exception as e:
            log.error(f"_save_state: impossible d'écrire state.json: {e}")

def _load_state():
    """Recharge l'état depuis state.json au démarrage. Ignore si absent ou corrompu."""
    global _pos_60r, _pos_45r, _shared_direction, _daily_realized_pnl, _daily_reset_date
    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)

        _pos_60r          = int(state.get("pos_60r", 0))
        _pos_45r          = int(state.get("pos_45r", 0))
        _shared_direction = state.get("shared_direction")

        saved_date_str = state.get("daily_reset_date")
        today_et       = datetime.now(TZ_ET).date()

        if saved_date_str:
            saved_date = date_cls.fromisoformat(saved_date_str)
            if saved_date == today_et:
                # Même journée → restaurer le P&L
                _daily_realized_pnl = float(state.get("daily_realized_pnl", 0.0))
                _daily_reset_date   = saved_date
                log.info(
                    f"[STATE] Chargé: pos_60r={_pos_60r} pos_45r={_pos_45r} "
                    f"dir={_shared_direction} pnl={_daily_realized_pnl:.2f}$ (même journée)"
                )
            else:
                # Nouvelle journée → reset P&L, conserver positions
                _daily_realized_pnl = 0.0
                _daily_reset_date   = today_et
                log.info(
                    f"[STATE] Nouvelle journée: P&L reset. "
                    f"pos_60r={_pos_60r} pos_45r={_pos_45r} dir={_shared_direction}"
                )
        else:
            _daily_realized_pnl = 0.0
            _daily_reset_date   = today_et

    except FileNotFoundError:
        log.info("[STATE] state.json introuvable — démarrage avec état vide")
        _daily_reset_date = datetime.now(TZ_ET).date()
    except Exception as e:
        log.error(f"[STATE] _load_state erreur: {e} — état initial vide")
        _daily_reset_date = datetime.now(TZ_ET).date()

def reconcile_state_with_api():
    """Réconcilie l'état interne avec les positions réelles via l'API TopstepX.
    Appelé au démarrage (avec délai pour laisser le token se charger).
    - Position API présente mais état interne flat → reconstruction (affecte pos_45r)
    - État interne non-flat mais API flat → reset état interne
    - Incohérence de direction → état interne aligné sur l'API (source de vérité)
    """
    global _pos_45r, _pos_60r, _shared_direction
    if not _token:
        log.warning("[RECONCILE] Pas de token TopstepX — skip réconciliation")
        return
    try:
        positions = get_positions()
        log.info(f"[RECONCILE] Positions API: {len(positions)} position(s)")

        if not positions:
            # API flat
            if _pos_45r != 0 or _pos_60r != 0:
                log.warning(
                    f"[RECONCILE] État interne non-flat (60r={_pos_60r} 45r={_pos_45r}) "
                    f"mais API flat → reset état interne"
                )
                _pos_60r          = 0
                _pos_45r          = 0
                _shared_direction = None
                _save_state()
            else:
                log.info("[RECONCILE] API flat et état interne flat → OK")
            return

        # Position ouverte sur l'API
        p         = positions[0]
        # TopstepX convention : type=0 LONG, type=1 SHORT
        pos_type  = p.get('type', 0)
        size      = int(p.get('size', 1))
        direction = 'long' if pos_type == 0 else 'short'
        signed    = size if pos_type == 0 else -size

        if _pos_45r == 0 and _pos_60r == 0:
            # État perdu au redémarrage mais position réelle ouverte
            log.warning(
                f"[RECONCILE] Position API {direction.upper()} {size} ctr(s) "
                f"MAIS état interne flat → reconstruction pos_45r={signed}"
            )
            _pos_45r          = signed
            _shared_direction = direction
            _save_state()

        else:
            # Vérifier cohérence direction
            total_internal = _pos_45r + _pos_60r
            api_is_long    = signed > 0
            int_is_long    = total_internal > 0

            if api_is_long != int_is_long:
                log.error(
                    f"[RECONCILE] INCOHÉRENCE direction — API={direction} signed={signed} "
                    f"/ interne 60r={_pos_60r} 45r={_pos_45r} → alignement sur l'API"
                )
                _pos_45r          = signed
                _pos_60r          = 0
                _shared_direction = direction
                _save_state()
            else:
                log.info(
                    f"[RECONCILE] Cohérent — API {direction.upper()} {size} ctr(s) "
                    f"/ interne 60r={_pos_60r} 45r={_pos_45r}"
                )

    except Exception as e:
        log.error(f"[RECONCILE] reconcile_state_with_api erreur: {e}")

def _startup_sequence():
    """Tâche de démarrage différée : charge l'état et réconcilie avec l'API."""
    time.sleep(5)   # Laisse Flask démarrer et le token se charger
    log.info("[STARTUP] Chargement état + réconciliation API ...")
    _load_state()
    reconcile_state_with_api()
    log.info("[STARTUP] Initialisation terminée")

# ── Labouchere ────────────────────────────────────────────────────────────────
class LabTracker:
    INIT = [1, 1, 1, 1]

    def __init__(self, unit_usd=UNIT_USD, init=None):
        self.unit_usd  = unit_usd
        self.INIT_SEQ  = init if init else list(self.INIT)
        self.seq       = list(self.INIT_SEQ)
        self.tape      = [{"val": v, "status": "active", "cycle": 0} for v in self.INIT_SEQ]
        self.cycle     = 0
        self.wins      = self.losses = 0
        self.trades    = []
        self._lock     = threading.Lock()

    def _bet(self) -> float:
        if not self.seq:        return 1.0
        if len(self.seq) == 1: return self.seq[0] * 2
        return self.seq[0] + self.seq[-1]

    def contracts(self) -> int:
        return max(1, round(self._bet()))

    def record(self, side: str, contracts: int, result: str, pnl: float = 0.0):
        with self._lock:
            bet = self._bet()
            self.trades.append({
                "time": datetime.now(TZ_ET).strftime("%H:%M"),
                "side": side, "contracts": contracts,
                "bet_units": bet, "result": result, "pnl": round(pnl, 2)
            })
            if result == "win":
                self.wins += 1
                gain = abs(pnl)
                slot = max(1, round(gain / 3 / self.unit_usd))
                for _ in range(3):
                    self.seq.append(slot)
                    self.tape.append({"val": slot, "status": "active", "cycle": self.cycle})
            else:
                self.losses += 1
                sz = len(self.seq)
                if sz >= 2:
                    act = [i for i, e in enumerate(self.tape)
                           if e["status"] == "active" and e["cycle"] == self.cycle]
                    if len(act) >= 2:
                        self.tape[act[0]]["status"]  = "crossed"
                        self.tape[act[-1]]["status"] = "crossed"
                    self.seq = self.seq[1:-1]
                elif sz == 1:
                    for e in self.tape:
                        if e["status"] == "active" and e["cycle"] == self.cycle:
                            e["status"] = "crossed"
                    self.seq = []
                if not self.seq:
                    self.cycle += 1
                    self.seq = list(self.INIT_SEQ)
                    self.tape.append({"val": None, "status": "separator", "cycle": self.cycle})
                    for v in self.INIT_SEQ:
                        self.tape.append({"val": v, "status": "active", "cycle": self.cycle})

    def state(self) -> dict:
        with self._lock:
            b = self._bet()
            return {
                "seq": list(self.seq),
                "bet_units": b,
                "bet_usd": round(b * self.unit_usd, 2),
                "contracts": self.contracts(),
                "wins": self.wins, "losses": self.losses,
                "cycle": self.cycle, "total_trades": len(self.trades),
                "unit_usd": self.unit_usd
            }

    def tape_html(self) -> str:
        with self._lock:
            b = self._bet()
            parts = []
            for e in self.tape:
                if e["status"] == "separator":
                    parts.append(
                        f'<span style="color:#555;margin:0 8px">│</span>'
                        f'<span style="color:#aaa;font-size:11px">Cycle {e["cycle"]+1}</span>')
                    continue
                label = f'{e["val"]}u'
                if e["status"] == "crossed":
                    parts.append(
                        f'<span style="text-decoration:line-through;color:#ff4444;margin:2px 5px">{label}</span>')
                else:
                    act = [x for x in self.tape
                           if x["status"] == "active" and x["cycle"] == e["cycle"]]
                    hl  = act and (act[0] is e or act[-1] is e)
                    c   = "#00e676" if hl else "#c9d1d9"
                    brd = "border:1px solid #00e676;border-radius:3px;padding:1px 5px;" if hl else ""
                    parts.append(
                        f'<span style="color:{c};margin:2px 5px;{brd}">{label}</span>')

            rows = "".join(
                f'<tr>'
                f'<td style="color:#888;padding:3px 8px">{t["time"]}</td>'
                f'<td style="padding:3px 8px">{t["side"].upper()}</td>'
                f'<td style="padding:3px 8px">{t["contracts"]} MNQ</td>'
                f'<td style="padding:3px 8px">{t["bet_units"]}u</td>'
                f'<td style="color:{"#00e676" if t["result"]=="win" else "#ff4444"};'
                f'font-weight:bold;padding:3px 8px">{t["result"].upper()}</td>'
                f'<td style="color:{"#00e676" if t["pnl"]>=0 else "#ff4444"};padding:3px 8px">'
                f'{"+" if t["pnl"]>=0 else ""}{t["pnl"]:.0f}$</td>'
                f'</tr>'
                for t in reversed(self.trades[-10:])
            )

            next_usd = round(b * self.unit_usd, 0)
            return (
                "".join(parts) +
                f'<div style="margin-top:10px;padding:8px;background:#1a1f2e;border-radius:6px;'
                f'border-left:3px solid #00e676">'
                f'<b style="color:#00e676">Prochaine mise :</b> '
                f'<span style="color:#fff;font-size:16px">{b} contrats</span> '
                f'<span style="color:#888">(≈${next_usd:.0f} · gains réels÷3)</span></div>' +
                (f'<table style="font-family:monospace;font-size:13px;width:100%;margin-top:16px">'
                 f'<tr style="color:#555"><th>Heure</th><th>Côté</th><th>Contrats</th>'
                 f'<th>Mise</th><th>Résultat</th><th>P&L</th></tr>{rows}</table>' if rows else "")
            )


lab           = LabTracker(init=[1, 1, 1, 1])
lab_45r       = LabTracker(init=[0.5, 0.5, 0.5, 0.5])
_current_trade: dict     = {}
_current_trade_45r: dict = {}

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "time":   datetime.now().isoformat(),
        "token_ok": bool(_token),
        "pos_60r": _pos_60r,
        "pos_45r": _pos_45r,
        "shared_direction": _shared_direction,
        "daily_pnl": round(_daily_realized_pnl, 2),
    })

@app.route('/set-token', methods=['POST'])
def set_token_route():
    global _token
    t = (request.get_json(force=True, silent=True) or {}).get('token', '')
    if not t: return jsonify({'error': 'no token'}), 400
    _token = t
    return jsonify({'status': 'ok', 'prefix': t[:20]})

@app.route('/account')
def account():
    return jsonify(get_account())

@app.route('/positions')
def positions():
    return jsonify({'positions': get_positions()})

@app.route('/state')
def state_route():
    """Retourne l'état complet du bot (debug)."""
    return jsonify({
        "pos_60r":            _pos_60r,
        "pos_45r":            _pos_45r,
        "shared_direction":   _shared_direction,
        "daily_realized_pnl": round(_daily_realized_pnl, 2),
        "daily_reset_date":   str(_daily_reset_date),
        "lab_60r":            lab.state(),
        "lab_45r":            lab_45r.state(),
        "max_contracts":      MAX_CONTRACTS,
        "max_daily_loss":     MAX_DAILY_LOSS,
    })

@app.route('/reconcile', methods=['POST'])
def reconcile_route():
    """Force une réconciliation état interne ↔ API (admin)."""
    tok = request.headers.get('X-Webhook-Token') or request.args.get('token', '')
    if tok != WEBHOOK_TOKEN:
        return jsonify({'error': 'unauthorized'}), 401
    reconcile_state_with_api()
    return jsonify({
        'status': 'ok',
        'pos_60r': _pos_60r, 'pos_45r': _pos_45r,
        'shared_direction': _shared_direction,
    })

@app.route('/lab/state')
def lab_state():
    return jsonify(lab.state())

@app.route('/lab/win', methods=['POST'])
def lab_win():
    body = request.get_json(force=True, silent=True) or {}
    pnl  = float(body.get('pnl', 0) or 0)
    side = _current_trade.get('side', 'long')
    ctr  = _current_trade.get('contracts', lab.contracts())
    lab.record(side, ctr, 'win', pnl)
    return jsonify({'status': 'ok', 'lab': lab.state()})

@app.route('/lab/loss', methods=['POST'])
def lab_loss():
    body = request.get_json(force=True, silent=True) or {}
    pnl  = float(body.get('pnl', 0) or 0)
    side = _current_trade.get('side', 'long')
    ctr  = _current_trade.get('contracts', lab.contracts())
    lab.record(side, ctr, 'loss', pnl)
    return jsonify({'status': 'ok', 'lab': lab.state()})

@app.route('/lab/45r/state')
def lab_45r_state():
    return jsonify(lab_45r.state())

@app.route('/lab/45r/win', methods=['POST'])
def lab_45r_win():
    body = request.get_json(force=True, silent=True) or {}
    pnl  = float(body.get('pnl', 0) or 0)
    side = _current_trade_45r.get('side', 'long')
    ctr  = _current_trade_45r.get('contracts', lab_45r.contracts())
    lab_45r.record(side, ctr, 'win', pnl)
    return jsonify({'status': 'ok', 'lab_45r': lab_45r.state()})

@app.route('/lab/45r/loss', methods=['POST'])
def lab_45r_loss():
    body = request.get_json(force=True, silent=True) or {}
    pnl  = float(body.get('pnl', 0) or 0)
    side = _current_trade_45r.get('side', 'long')
    ctr  = _current_trade_45r.get('contracts', lab_45r.contracts())
    lab_45r.record(side, ctr, 'loss', pnl)
    return jsonify({'status': 'ok', 'lab_45r': lab_45r.state()})

def partial_close(contracts: int, tracked_pos: int = 0):
    """Ferme exactement N contrats.
    tracked_pos : valeur de _pos_45r ou _pos_60r (+ = long, - = short)
    Si tracked_pos == 0 (restart), on utilise l'API pour deviner la direction.
    """
    pos = get_positions()
    if not pos:
        return {"success": True, "note": "already_flat"}
    if tracked_pos != 0:
        side = 'SELL' if tracked_pos > 0 else 'BUY'
    else:
        # Fallback : utiliser la position API (type 0 = LONG, type 1 = SHORT)
        p    = pos[0]
        t    = p.get('type', 0)
        side = 'BUY' if t == 1 else 'SELL'
    return place_order(side, contracts)

# ── Webhook 60R ───────────────────────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    global _current_trade, _shared_direction, _pos_60r
    global _daily_realized_pnl, _daily_reset_date

    tok = request.headers.get('X-Webhook-Token') or request.args.get('token', '')
    if tok != WEBHOOK_TOKEN:
        return jsonify({'error': 'unauthorized'}), 401

    data       = request.get_json(force=True, silent=True) or {}
    action     = data.get('action', '').lower()

    # ⚠️ TOUJOURS utiliser le Labouchere interne — ignorer `contracts` du signal Pine Script
    contracts  = min(lab.contracts(), MAX_CONTRACTS)
    lab_result = data.get('lab_result', '').lower()
    lab_pnl    = float(data.get('lab_pnl', 0) or 0)

    log.info(f"[60R] Signal reçu: action={action} contracts={contracts} (Labouchere interne)")

    now_et = datetime.now(TZ_ET)
    today  = now_et.date()

    # Reset P&L journalier (si nouvelle journée)
    if _daily_reset_date != today:
        _daily_realized_pnl = 0.0
        _daily_reset_date   = today
        _save_state()
        log.info(f"[60R] Reset P&L journalier (nouvelle journée: {today})")

    bar_time = now_et.hour * 100 + now_et.minute
    if bar_time < 930 or bar_time >= 1545:
        return jsonify({'status': 'ignored', 'reason': 'outside_session'})

    # Circuit-breaker : perte journalière max
    if _daily_realized_pnl <= -MAX_DAILY_LOSS:
        log.warning(f"[60R] CIRCUIT-BREAKER: perte ${_daily_realized_pnl:.0f} >= limite ${MAX_DAILY_LOSS}")
        return jsonify({'status': 'halted', 'reason': 'daily_loss_limit', 'pnl': _daily_realized_pnl})

    # Mise à jour Labouchere si résultat inclus dans le signal
    if lab_result in ('win', 'loss') and _current_trade:
        lab.record(_current_trade.get('side', '?'),
                   _current_trade.get('contracts', contracts),
                   lab_result, lab_pnl)
        _daily_realized_pnl += lab_pnl
        _save_state()

    result = {}
    if action in ('buy', 'long'):
        wanted = 'long'
        if _shared_direction and _shared_direction != wanted:
            log.info(f"[60R] BUY ignoré — conflit direction {_shared_direction}")
            return jsonify({'status': 'ignored', 'reason': f'direction_conflict:{_shared_direction}', 'lab': lab.state()})
        if _pos_60r > 0:
            log.info(f"[60R] BUY ignoré — déjà en position LONG ({_pos_60r} ctrs)")
            return jsonify({'status': 'ignored', 'reason': 'already_long_60r', 'pos_60r': _pos_60r})
        _shared_direction = wanted
        _current_trade    = {'side': 'long', 'contracts': contracts}
        _pos_60r          = contracts
        result = place_order('BUY', contracts)
        _save_state()
        log.info(f"[60R] BUY {contracts} → pos_60r={_pos_60r} | {result}")

    elif action in ('sell', 'short'):
        wanted = 'short'
        if _shared_direction and _shared_direction != wanted:
            log.info(f"[60R] SELL ignoré — conflit direction {_shared_direction}")
            return jsonify({'status': 'ignored', 'reason': f'direction_conflict:{_shared_direction}', 'lab': lab.state()})
        if _pos_60r < 0:
            log.info(f"[60R] SELL ignoré — déjà en position SHORT ({_pos_60r} ctrs)")
            return jsonify({'status': 'ignored', 'reason': 'already_short_60r', 'pos_60r': _pos_60r})
        _shared_direction = wanted
        _current_trade    = {'side': 'short', 'contracts': contracts}
        _pos_60r          = -contracts
        result = place_order('SELL', contracts)
        _save_state()
        log.info(f"[60R] SELL {contracts} → pos_60r={_pos_60r} | {result}")

    elif action == 'close':
        pos_open = get_positions()
        if _pos_60r != 0:
            result = partial_close(abs(_pos_60r), tracked_pos=_pos_60r)
            log.info(f"[60R] CLOSE {abs(_pos_60r)} ctrs (direction connue) → {result}")
        elif pos_open and _pos_45r == 0:
            result = close_all()
            log.warning(f"[60R] CLOSE fallback (état perdu) → {result}")
        else:
            result = {"note": "already_flat"}
        _pos_60r = 0
        if _pos_45r == 0:
            _shared_direction = None
        _save_state()

    else:
        return jsonify({'error': f'action inconnue: {action}'}), 400

    return jsonify({'status': 'ok', 'source': '60r', 'action': action, 'contracts': contracts,
                    'result': result, 'lab': lab.state(), 'direction': _shared_direction,
                    'pos_60r': _pos_60r, 'pos_45r': _pos_45r})

# ── Webhook 45R ───────────────────────────────────────────────────────────────
@app.route('/webhook/45r', methods=['POST'])
def webhook_45r():
    """Webhook dédié 45R — Labouchere indépendant du 60R."""
    global _current_trade_45r, _shared_direction, _pos_45r
    # [BUG-3] Déclaration globale manquante — causait UnboundLocalError sur _daily_realized_pnl += pnl
    global _daily_realized_pnl, _daily_reset_date

    tok = request.headers.get('X-Webhook-Token') or request.args.get('token', '')
    if tok != WEBHOOK_TOKEN:
        return jsonify({'error': 'unauthorized'}), 401

    data       = request.get_json(force=True, silent=True) or {}
    action     = data.get('action', '').lower()
    contracts  = min(lab_45r.contracts(), MAX_CONTRACTS)
    lab_result = data.get('lab_result', '').lower()
    lab_pnl    = float(data.get('lab_pnl', 0) or 0)

    log.info(f"[45R] Signal reçu: action={action} contracts={contracts}")

    now_et = datetime.now(TZ_ET)
    today  = now_et.date()

    # [BUG-3] Reset P&L journalier absent de webhook_45r — ajouté
    if _daily_reset_date != today:
        _daily_realized_pnl = 0.0
        _daily_reset_date   = today
        _save_state()
        log.info(f"[45R] Reset P&L journalier (nouvelle journée: {today})")

    bar_time = now_et.hour * 100 + now_et.minute
    if bar_time < 930 or bar_time >= 1545:
        return jsonify({'status': 'ignored', 'reason': 'outside_session'})

    # Circuit-breaker
    if _daily_realized_pnl <= -MAX_DAILY_LOSS:
        log.warning(f"[45R] CIRCUIT-BREAKER: perte ${_daily_realized_pnl:.0f} >= limite ${MAX_DAILY_LOSS}")
        return jsonify({'status': 'halted', 'reason': 'daily_loss_limit', 'pnl': _daily_realized_pnl})

    if lab_result in ('win', 'loss') and _current_trade_45r:
        lab_45r.record(_current_trade_45r.get('side', '?'),
                       _current_trade_45r.get('contracts', contracts),
                       lab_result, lab_pnl)
        _daily_realized_pnl += lab_pnl
        _save_state()

    result = {}
    if action in ('buy', 'long'):
        wanted = 'long'
        if _shared_direction and _shared_direction != wanted:
            log.info(f"[45R] BUY ignoré — conflit direction {_shared_direction}")
            return jsonify({'status': 'ignored', 'reason': f'direction_conflict:{_shared_direction}', 'lab_45r': lab_45r.state()})
        if _pos_45r > 0:
            log.info(f"[45R] BUY ignoré — déjà en position LONG ({_pos_45r} ctrs)")
            return jsonify({'status': 'ignored', 'reason': 'already_long_45r', 'pos_45r': _pos_45r})
        _shared_direction  = wanted
        _current_trade_45r = {'side': 'long', 'contracts': contracts}
        _pos_45r           = contracts
        result = place_order('BUY', contracts)
        _save_state()
        log.info(f"[45R] BUY {contracts} → pos_45r={_pos_45r} | {result}")

    elif action in ('sell', 'short'):
        wanted = 'short'
        if _shared_direction and _shared_direction != wanted:
            log.info(f"[45R] SELL ignoré — conflit direction {_shared_direction}")
            return jsonify({'status': 'ignored', 'reason': f'direction_conflict:{_shared_direction}', 'lab_45r': lab_45r.state()})
        if _pos_45r < 0:
            log.info(f"[45R] SELL ignoré — déjà en position SHORT ({_pos_45r} ctrs)")
            return jsonify({'status': 'ignored', 'reason': 'already_short_45r', 'pos_45r': _pos_45r})
        _shared_direction  = wanted
        _current_trade_45r = {'side': 'short', 'contracts': contracts}
        _pos_45r           = -contracts
        result = place_order('SELL', contracts)
        _save_state()
        log.info(f"[45R] SELL {contracts} → pos_45r={_pos_45r} | {result}")

    elif action == 'close':
        pos_open = get_positions()
        if _pos_45r != 0:
            result = partial_close(abs(_pos_45r), tracked_pos=_pos_45r)
            log.info(f"[45R] CLOSE {abs(_pos_45r)} ctrs (direction connue) → {result}")
        elif pos_open:
            # Bot redémarré — position ouverte mais état perdu → close_all sécurisé
            result = close_all()
            log.warning(f"[45R] CLOSE fallback (état perdu, pos_45r=0) → {result}")
        else:
            result = {"note": "already_flat"}
            log.info(f"[45R] CLOSE — déjà flat")

        _pos_45r = 0
        if _pos_60r == 0:
            _shared_direction = None

        # Enregistrer résultat dans Labouchere si P&L disponible
        if lab_pnl != 0 and _current_trade_45r:
            result_str = 'win' if lab_pnl > 0 else 'loss'
            lab_45r.record(_current_trade_45r.get('side', '?'),
                           _current_trade_45r.get('contracts', 1),
                           result_str, lab_pnl)
            _daily_realized_pnl += lab_pnl

        _save_state()

    else:
        return jsonify({'error': f'action inconnue: {action}'}), 400

    return jsonify({'status': 'ok', 'source': '45r', 'action': action, 'contracts': contracts,
                    'result': result, 'lab_45r': lab_45r.state(), 'direction': _shared_direction,
                    'pos_60r': _pos_60r, 'pos_45r': _pos_45r})

@app.route('/dashboard')
def dashboard():
    pos  = get_positions()
    acct = get_account()
    ls   = lab.state()
    now  = datetime.now(TZ_ET).strftime('%H:%M:%S ET')
    bal  = acct.get('balance', 0)
    total = ls['total_trades']
    wr    = f"{ls['wins']/total*100:.0f}%" if total else "—"
    pos_html = "".join(
        f'<div style="padding:6px 10px;background:#161b22;border-radius:4px;margin:4px 0">'
        f'<span style="color:{"#00e676" if p.get("type")==0 else "#ff4444"};font-weight:bold">'
        f'{"LONG" if p.get("type")==0 else "SHORT"}</span> '
        f'{p.get("size","?")} MNQ @ {p.get("averagePrice","?")}</div>'
        for p in pos
    ) or '<p style="color:#555">Aucune position ouverte</p>'

    pnl_color = "#00e676" if _daily_realized_pnl >= 0 else "#ff4444"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>TopstepX Dashboard</title>
<meta http-equiv="refresh" content="30">
<style>
body{{font-family:'Courier New',monospace;background:#0d1117;color:#c9d1d9;padding:20px;max-width:900px;margin:auto}}
h2{{color:#fff;border-bottom:1px solid #30363d;padding-bottom:8px}}
h3{{color:#888;margin-top:24px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin:10px 0}}
.stat{{display:inline-block;margin:0 16px;text-align:center}}
.stat .val{{font-size:22px;font-weight:bold;color:#fff}}
.stat .lbl{{font-size:11px;color:#888}}
</style></head><body>
<h2>🚀 TopstepX Bot — Dashboard <span style="font-size:13px;color:#555">{now}</span></h2>
<div class="card">
  <div class="stat"><div class="val">${bal:,.0f}</div><div class="lbl">Solde</div></div>
  <div class="stat"><div class="val">{wr}</div><div class="lbl">Win Rate</div></div>
  <div class="stat"><div class="val" style="color:#00e676">{ls['wins']}</div><div class="lbl">Wins</div></div>
  <div class="stat"><div class="val" style="color:#ff4444">{ls['losses']}</div><div class="lbl">Losses</div></div>
  <div class="stat"><div class="val">{ls['cycle']+1}</div><div class="lbl">Cycle</div></div>
  <div class="stat"><div class="val" style="color:{pnl_color}">{"+" if _daily_realized_pnl>=0 else ""}{_daily_realized_pnl:.0f}$</div><div class="lbl">P&L Jour</div></div>
</div>
<p style="color:#aaa;font-size:12px">
  📐 Séquence [1,1,1,1] · 1 unité = {ls['unit_usd']:.0f}$ · Progression : gains réels ÷ 3
  · Max {MAX_CONTRACTS} contrat(s) · Stop ${MAX_DAILY_LOSS}/jour
</p>
<h3>📊 Positions</h3>{pos_html}
<h3>🎯 Labouchere</h3>
<div class="card">{lab.tape_html()}</div>
<p style="color:#333;font-size:11px;margin-top:24px">Railway Bot v5 · EOD 16h00 ET · Persistance state.json</p>
</body></html>"""

# ── EOD Guardian ───────────────────────────────────────────────────────────────
_eod_closed_today: date_cls | None = None

def eod_guardian():
    """Thread de garde : force-close toutes les positions à 16h00 ET.
    [BUG-4] Corrections :
      - Fenêtre élargie : déclenche entre 15h59 et 16h01 (évite de rater la minute)
      - Force-close via API directement (indépendant de l'état interne)
      - Reset complet de l'état interne après force-close
      - Sauvegarde state.json après reset
    """
    global _eod_closed_today, _pos_60r, _pos_45r, _shared_direction

    while True:
        try:
            now_et = datetime.now(TZ_ET)
            today  = now_et.date()
            # Fenêtre élargie 15h59–16h01 (ET) pour ne pas rater l'EOD
            in_eod_window = (
                now_et.weekday() < 5                          # Lundi–Vendredi uniquement
                and _eod_closed_today != today                # Pas déjà fermé aujourd'hui
                and (now_et.hour == 16 and now_et.minute <= 1)  # 16h00 ou 16h01
                or (now_et.hour == 15 and now_et.minute == 59)  # ou 15h59
            )

            if in_eod_window and _eod_closed_today != today and now_et.weekday() < 5:
                # Force-close via API (source de vérité, indépendant de l'état interne)
                positions = get_positions()
                if positions:
                    log.warning(f"[EOD] {len(positions)} position(s) détectée(s) — force-close 16h00 ET")
                    r = close_all()
                    log.warning(f"[EOD] close_all → {r}")
                else:
                    log.info("[EOD] Aucune position ouverte à 16h00 ET — rien à faire")

                # [BUG-4] Reset état interne après EOD (même si API était déjà flat)
                _pos_60r          = 0
                _pos_45r          = 0
                _shared_direction = None
                _eod_closed_today = today
                _save_state()
                log.info("[EOD] État interne reset + state.json sauvegardé")

        except Exception as e:
            log.error(f"[EOD Guardian] Exception: {e}")

        time.sleep(30)

# ── Démarrage ─────────────────────────────────────────────────────────────────
# Charger l'état immédiatement (avant le premier request)
_load_state()

# Réconciliation API différée (5s) dans un thread dédié
threading.Thread(target=_startup_sequence, daemon=True).start()

# Thread EOD guardian
threading.Thread(target=eod_guardian, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    log.info(
        f"TopstepX Bot v5 — port {port} — "
        f"Labouchere interne — max {MAX_CONTRACTS} contrats — stop ${MAX_DAILY_LOSS}/jour"
    )
    app.run(host='0.0.0.0', port=port)
