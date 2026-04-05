#!/usr/bin/env python3
"""
DERIV RISE/FALL BOT
EMA30 trend + Bollinger Bands(45,2) + Swing entries
"""

import asyncio, json, ssl, sys, os, re, time, threading
from collections import deque
from datetime import datetime
from pathlib import Path

# ── Colours ──────────────────────────────────────────────────────────────────
R   = "\033[0m"
BLD = "\033[1m"
def fg(r,g,b): return f"\033[38;2;{r};{g};{b}m"

GRN = fg(0,255,160)
RED = fg(255,60,100)
YLW = fg(245,196,0)
BLU = fg(77,166,255)
CYN = fg(0,220,220)
WHT = fg(232,238,255)
DIM = fg(255,180,100)   # light orange for muted text
ORG = fg(255,140,0)

ANSI_RE = re.compile(r'\033\[[0-9;]*[a-zA-Z]')
def plain(s): return ANSI_RE.sub('', s)
def cell(s, w, c=WHT):
    """Render s in colour c, truncated/padded to exactly w visible characters."""
    p = plain(str(s))[:w]          # visible text, max w chars
    return f"{c}{p}{R}{' '*(w-len(p))}"  # pad with plain spaces after reset

# ── Config ────────────────────────────────────────────────────────────────────
WS_HOST      = "ws.binaryws.com"
WS_PATH      = "/websockets/v3?app_id=1089"
CANDLE_TF    = 60
CONTRACT_DUR = 2
CANDLE_COUNT = 150
EMA_FAST     = 21
EMA_TREND    = 30
BB_PERIOD    = 45
BB_DEV       = 2.0
ATR_PERIOD   = 14
SWING_LB     = 2
MAX_STREAK   = 3

KNOWN_SYMS = [
    "R_10","R_25","R_50","R_75","R_100",
    "1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V",
    "stpRNG","stpRNG2","stpRNG3","stpRNG4","stpRNG5",
]
_SYM_MAP = {s.lower(): s for s in KNOWN_SYMS}
def norm_sym(s): return _SYM_MAP.get(s.strip().lower(), s.strip())

# ── State ─────────────────────────────────────────────────────────────────────
S = {
    "token": "", "balance": 0.0, "currency": "USD", "account_type": "",
    "connected": False, "authorized": False,
    "running": False, "paused": False,
    "stopped_reason": "",
    "active_syms": [], "stakes": {},
    "candles": {}, "analysis": {},
    "last_tick": {}, "signals": {},
    "open_contracts": {}, "trades": [],
    # ── Proposal / fingerprint tracking (mirrors reference send_proposal flow) ─
    "proposals":            {},   # proposal_id → echo_req data
    "proposal_fps":         {},   # proposal_id → fingerprint
    "pending_fingerprints": {},   # contract_id → fingerprint
    "symbol_state":         {},   # symbol → {"pending_fps": [...]}
    # ── Contract tracking ────────────────────────────────────────────────────
    "contract_history":     {},   # contract_id → full trade record
    "active_setup_keys":    set(),# setup keys currently open — dedup guard
    # ── Trade memory (loaded from win.json / loss.json at startup) ───────────
    "win_memory":  {},            # setup_key → [records...]
    "loss_memory": {},            # setup_key → [records...]
    # ─────────────────────────────────────────────────────────────────────────
    "pnl": 0.0, "wins": 0, "losses": 0, "streak": 0,
    "daily_limit": 0.0,
    # ── TP / SL / Stake mode ─────────────────────────────────────────────────
    "stake_mode": "manual",   # "manual" | "auto"
    "session_tp":  0.0,       # take-profit target (session PnL >= tp → stop)
    "session_sl":  0.0,       # stop-loss limit   (session PnL <= -sl → stop)
    "default_stake": 0.0,     # stake used when stake_mode == "manual"
    # ── Concurrency limits ────────────────────────────────────────────────────
    "max_trades_per_sym": 1,  # simultaneous open trades allowed per symbol
    "max_syms_trading":   3,  # symbols allowed to have open trades at once
    # Slot counter — incremented when proposal sent, decremented when contract
    # settles. Tracked per symbol so the check is accurate even before Deriv
    # sends back the buy confirmation (open_contracts lags by one round-trip).
    "sym_open_slots":     {},  # sym -> int count of currently claimed slots
    # Map contract_id -> sym so we know which symbol's slot to release
    "cid_to_sym":         {},  # contract_id -> sym
    # ─────────────────────────────────────────────────────────────────────────
    "log": deque(maxlen=60),
    "ws": None, "req_id": 0,
}

# ── Paths — shared across all users (BASE_DIR set by front.py env) ───────────
BASE_DIR  = os.environ.get("TBOT_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
CMD_FILE  = os.environ.get("TBOT_CMD_FILE", "")  # written by front.py per-user
LOG_FILE  = os.path.join(BASE_DIR, "errors.log")
WIN_FILE  = os.path.join(BASE_DIR, "win.json")   # shared cross-user memory
LOSS_FILE = os.path.join(BASE_DIR, "loss.json")  # shared cross-user memory
# ── Config from front.py (app_id / api_token) ────────────────────────────────
_cfg_file = os.environ.get("TBOT_CONFIG", "")

# ── File logger (errors.log — always flushed, plain text, no ANSI) ────────────

def log_file(kind, msg):
    """Append a plain-text line to errors.log regardless of terminal state."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{kind.upper():8s}] {msg}\n")
    except Exception:
        pass  # never let file I/O crash the bot

# ── Trade memory helpers ──────────────────────────────────────────────────────
# WIN_FILE and LOSS_FILE defined above using BASE_DIR

def _setup_key(sym, sig, setup, struct):
    """Pattern key — symbol-agnostic so memory matches the same setup across all symbols."""
    return f"{sig}|{setup}|{struct}"

def _dedup_key(sym, setup_key):
    """Runtime dedup key — per-symbol so two symbols can trade the same pattern simultaneously."""
    return f"{sym}|{setup_key}"

# File-level lock so concurrent bot processes don't corrupt the shared JSON
_mem_lock = threading.Lock()

def _load_memory():
    """Load win/loss JSON files into S — shared across all user sessions."""
    for fpath, key in ((WIN_FILE, "win_memory"), (LOSS_FILE, "loss_memory")):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                S[key] = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            S[key] = {}

def _save_trade(outcome, record):
    """
    Append record to shared win/loss JSON using a cross-process-safe pattern:
      1. Acquire threading lock (guards against two async tasks in this process)
      2. Re-read the file from disk (picks up writes from other user processes)
      3. Merge our record in
      4. Write atomically via tmp + rename
    This means all users' trades accumulate in the same shared memory files.
    """
    key   = record["setup_key"]
    mkey  = "win_memory" if outcome == "win" else "loss_memory"
    fpath = WIN_FILE     if outcome == "win" else LOSS_FILE

    with _mem_lock:
        # Re-read from disk to get any writes from other bot processes
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                disk_mem = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            disk_mem = {}

        # Merge: append our record to the on-disk state
        if key not in disk_mem:
            disk_mem[key] = []
        disk_mem[key].append(record)

        # Update in-process memory so it stays consistent
        S[mkey] = disk_mem

        # Write atomically — tmp file then rename prevents partial reads
        tmp = fpath + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(disk_mem, f, indent=2)
            os.replace(tmp, fpath)
        except Exception as e:
            log_file("error", f"_save_trade write {fpath}: {e}")

def _memory_summary(setup_key):
    """
    Return (wins, losses, last_outcome, recent_loss_streak) for a setup key.

    Reads from the in-process cache (S["win_memory"] / S["loss_memory"]) which
    _save_trade() keeps in sync with the shared disk files after every settled
    contract — so this always reflects the latest cross-user data.

    recent_loss_streak: number of consecutive losses at the tail of the combined
    timeline, estimated by comparing the latest record timestamps in each list.
    We approximate by counting how many of the last N records are losses.
    """
    wl = S["win_memory"].get(setup_key, [])
    ll = S["loss_memory"].get(setup_key, [])
    w, l = len(wl), len(ll)

    last = None
    if w and l:
        last = "win" if w >= l else "loss"
    elif w:
        last = "win"
    elif l:
        last = "loss"

    # Count recent consecutive losses in the last 5 trades (approximated)
    # by looking at the tail of the loss list relative to total count
    recent_losses = 0
    total = w + l
    if total >= 2:
        # If losses dominate the most recent portion: l / total in last N
        tail = min(5, total)
        recent_l = min(l, tail)  # worst case: all recent trades are losses
        # Refine: if w is large relative to l the tail is probably not all losses
        if w > 0:
            recent_l = max(0, tail - round(tail * w / total))
        recent_losses = recent_l

    return w, l, last, recent_losses


def _should_skip_on_memory(setup_key):
    """
    Decide whether to skip a trade based on historical memory for this setup.

    Block the trade if ANY of:
      1. ≥ 3 total trades AND loss rate > 60%  (persistently bad setup)
      2. ≥ 2 total trades AND last outcome was a loss AND recent_loss_streak ≥ 2
         (don't repeat a freshly losing pattern back-to-back)

    Refresh in-process memory from disk before checking so we always use the
    latest shared data written by other concurrent user processes.
    """
    # Re-read from disk to pick up any records written by other user processes
    # since this bot started or last traded.
    for fpath, key in ((WIN_FILE, "win_memory"), (LOSS_FILE, "loss_memory")):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                S[key] = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass   # keep existing in-process data if file unreadable

    w, l, last_outcome, recent_losses = _memory_summary(setup_key)
    total = w + l

    if total == 0:
        return False, "no history"   # no data — always allow

    loss_rate = l / total

    # Rule 1: persistently losing setup
    if total >= 3 and loss_rate > 0.60:
        return True, f"blocked: {w}W/{l}L ({int(loss_rate*100)}% loss rate)"

    # Rule 2: back-to-back losses on a fresh setup
    if total >= 2 and last_outcome == "loss" and recent_losses >= 2:
        return True, f"blocked: last outcome loss, {recent_losses} recent losses"

    return False, f"allowed: {w}W/{l}L last={last_outcome}"


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg, kind="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    c = {"info":WHT,"ok":GRN,"error":RED,"trade":CYN,
         "win":GRN,"loss":RED,"signal":YLW,"warn":ORG}.get(kind, WHT)
    i = {"info":"·","ok":"✓","error":"✗","trade":"◆",
         "win":"▲","loss":"▼","signal":"⚡","warn":"⚠"}.get(kind, "·")
    S["log"].appendleft(f"{DIM}{ts}{R} {c}{i} {msg}{R}")
    # Mirror everything to file — errors/warnings/signals/trades all captured
    log_file(kind, msg)

# ── Analysis ──────────────────────────────────────────────────────────────────
def ema(vals, p):
    k, e, out = 2/(p+1), None, []
    for v in vals:
        e = v if e is None else v*k + e*(1-k)
        out.append(e)
    return out

def sma(vals, p):
    out = []
    for i in range(len(vals)):
        w = vals[max(0,i-p+1):i+1]
        out.append(sum(w)/len(w))
    return out

def bbands(vals, p=BB_PERIOD, d=BB_DEV):
    mid = sma(vals, p)
    up, lo = [], []
    for i in range(len(vals)):
        w = vals[max(0,i-p+1):i+1]
        std = (sum((x-mid[i])**2 for x in w)/len(w))**0.5
        up.append(mid[i]+d*std); lo.append(mid[i]-d*std)
    return up, mid, lo

def calc_atr(bars, p=ATR_PERIOD):
    trs = [bars[0]["high"]-bars[0]["low"]]
    for i in range(1, len(bars)):
        b,pb = bars[i], bars[i-1]
        trs.append(max(b["high"]-b["low"],
                       abs(b["high"]-pb["close"]),
                       abs(b["low"]-pb["close"])))
    prev = sum(trs[:p])/p if len(trs)>=p else sum(trs)/len(trs)
    res = [prev]
    for t in trs[p:]:
        prev = (prev*(p-1)+t)/p; res.append(prev)
    return res

def swings(bars, lb=SWING_LB):
    hi, lo = [], []
    for i in range(lb, len(bars)-lb):
        if all(bars[j]["high"] < bars[i]["high"] for j in range(i-lb,i+lb+1) if j!=i):
            hi.append({"i":i,"p":bars[i]["high"]})
        if all(bars[j]["low"]  > bars[i]["low"]  for j in range(i-lb,i+lb+1) if j!=i):
            lo.append({"i":i,"p":bars[i]["low"]})
    return hi, lo

def update_structure(sym, bars, hi, lo):
    """
    Progressive BOS / CHoCH structure tracker.

    Maintains a running state in S["symbol_state"][sym]["struct_state"] so
    structure evolves bar-by-bar rather than being recomputed from scratch
    from only the last two confirmed pivots.

    Logic
    ─────
    We track two reference levels that shift as price confirms new swings:
      • ref_high  — the most recently confirmed swing high
      • ref_low   — the most recently confirmed swing low
      • bias      — current structural bias: "bull" | "bear" | "ranging"

    On each call (one per bar):
      1. Integrate any new confirmed swing highs/lows that the pivot detector
         has identified since the last update.
      2. Check whether the current close has broken above ref_high (BOS bull)
         or below ref_low (BOS bear) relative to the existing bias — a
         continuation break.
      3. Check for CHoCH — price breaking the *opposite* reference level
         against the current bias (change of character / structural flip).
      4. Update bias and reference levels accordingly.
      5. Return a struct string + bos/choch flags consumed by analyse().

    Returns dict: {struct, bos, choch}
      struct: "BULLISH" | "BEARISH" | "CHOCH_BULL" | "CHOCH_BEAR" | "RANGING"
      bos:    True when a clean continuation break just occurred
      choch:  True when structure just flipped direction
    """
    state = _sym_state(sym)
    ss    = state.setdefault("struct_state", {
        "bias":     "ranging",
        "ref_high": None,   # last confirmed swing high price
        "ref_low":  None,   # last confirmed swing low price
        "ref_hi_i": -1,     # bar index of ref_high
        "ref_lo_i": -1,     # bar index of ref_low
        "last_hi_i": -1,    # highest bar index seen from hi list
        "last_lo_i": -1,    # highest bar index seen from lo list
        "struct":   "RANGING",
        "bos":      False,
        "choch":    False,
    })

    curr  = bars[-1]["close"]
    curr_i = len(bars) - 1

    # ── 1. Integrate new confirmed swing pivots ───────────────────────────────
    # Only process pivots we haven't seen yet (index > last seen)
    new_highs = [p for p in hi if p["i"] > ss["last_hi_i"]]
    new_lows  = [p for p in lo if p["i"] > ss["last_lo_i"]]

    if new_highs:
        ss["last_hi_i"] = new_highs[-1]["i"]
        # Update ref_high to the latest confirmed swing high
        if ss["ref_high"] is None or new_highs[-1]["p"] != ss["ref_high"]:
            ss["ref_high"] = new_highs[-1]["p"]
            ss["ref_hi_i"] = new_highs[-1]["i"]

    if new_lows:
        ss["last_lo_i"] = new_lows[-1]["i"]
        # Update ref_low to the latest confirmed swing low
        if ss["ref_low"] is None or new_lows[-1]["p"] != ss["ref_low"]:
            ss["ref_low"] = new_lows[-1]["p"]
            ss["ref_lo_i"] = new_lows[-1]["i"]

    # Need at least one reference level on each side to track structure
    if ss["ref_high"] is None or ss["ref_low"] is None:
        ss["struct"] = "RANGING"; ss["bos"] = False; ss["choch"] = False
        return {"struct": "RANGING", "bos": False, "choch": False}

    ref_h = ss["ref_high"]
    ref_l = ss["ref_low"]
    bias  = ss["bias"]
    bos   = False
    choch = False

    # ── 2. BOS — break in the direction of current bias ───────────────────────
    if bias == "bull" and curr > ref_h:
        # Bullish BOS: close above last confirmed swing high → structure holds
        bos = True
        ss["ref_high"] = curr          # new reference high is the live close
        ss["ref_hi_i"] = curr_i

    elif bias == "bear" and curr < ref_l:
        # Bearish BOS: close below last confirmed swing low
        bos = True
        ss["ref_low"] = curr
        ss["ref_lo_i"] = curr_i

    # ── 3. CHoCH — break against the current bias ────────────────────────────
    elif bias == "bull" and curr < ref_l:
        # Price broke below the last swing low while in bullish bias → flip
        choch = True
        bias  = "bear"
        ss["ref_low"] = curr
        ss["ref_lo_i"] = curr_i

    elif bias == "bear" and curr > ref_h:
        # Price broke above the last swing high while in bearish bias → flip
        choch = True
        bias  = "bull"
        ss["ref_high"] = curr
        ss["ref_hi_i"] = curr_i

    # ── 4. Initial bias assignment (ranging → first directional read) ─────────
    elif bias == "ranging":
        if len(hi) >= 2 and len(lo) >= 2:
            hh = hi[-1]["p"] > hi[-2]["p"]
            hl = lo[-1]["p"] > lo[-2]["p"]
            lh = hi[-1]["p"] < hi[-2]["p"]
            ll = lo[-1]["p"] < lo[-2]["p"]
            if hh and hl:   bias = "bull"
            elif lh and ll: bias = "bear"
            # else stay ranging

    # ── 5. Persist and return ─────────────────────────────────────────────────
    ss["bias"]  = bias
    ss["bos"]   = bos
    ss["choch"] = choch

    if choch and bias == "bull":   struct = "CHOCH_BULL"
    elif choch and bias == "bear": struct = "CHOCH_BEAR"
    elif bias == "bull":           struct = "BULLISH"
    elif bias == "bear":           struct = "BEARISH"
    else:                          struct = "RANGING"

    ss["struct"] = struct
    return {"struct": struct, "bos": bos, "choch": choch}

def exh_score(bars, ema_v):
    if len(bars)<10: return 0
    atr_v = calc_atr(bars)[-1] or 1
    last  = bars[-1]
    rng   = last["high"]-last["low"] or 1e-9
    bull  = last["close"]>=last["open"]
    wr    = (min(last["open"],last["close"])-last["low"])/rng if bull else (last["high"]-max(last["open"],last["close"]))/rng
    nd    = abs((bars[-1]["close"]-ema_v[-1])/atr_v)
    orig  = bars[-10]["close"]
    aext  = abs(bars[-1]["close"]-orig)/atr_v
    n     = 6; recent = bars[-n:]
    bodies= [abs(b["close"]-b["open"])/((b["high"]-b["low"]) or 1e-9) for b in recent]
    h2    = n//2
    early = sum(bodies[:h2])/h2 if h2 else 0
    late  = sum(bodies[h2:])/(n-h2)
    vd    = max(0,(early-late)/early) if early>0 else 0
    return round(min(aext/3,1)*30 + vd*25 + wr*25 + min(nd/1.2,1)*20, 1)

# ── Confirmation Patterns ─────────────────────────────────────────────────────
def _candle_body(b):
    return abs(b["close"] - b["open"])

def _candle_range(b):
    return (b["high"] - b["low"]) or 1e-9

def _is_bullish(b):
    return b["close"] > b["open"]

def _is_bearish(b):
    return b["close"] < b["open"]

def _body_ratio(b):
    return _candle_body(b) / _candle_range(b)

def _upper_wick(b):
    return b["high"] - max(b["open"], b["close"])

def _lower_wick(b):
    return min(b["open"], b["close"]) - b["low"]

def confirm_reversal_bull(bars, atr_v):
    """
    Confirm a bullish reversal at lower/upper-middle band.
    Requires BOTH:
      1. Momentum shift: current candle shows bullish body and ATR expanding
      2. Pattern: bullish engulfing OR hammer OR swing higher low formed
    Returns (confirmed: bool, pattern_name: str)
    """
    if len(bars) < 3:
        return False, ""
    c0 = bars[-1]   # current (signal) candle
    c1 = bars[-2]   # previous candle
    c2 = bars[-3]   # two bars ago

    rng0  = _candle_range(c0)
    rng1  = _candle_range(c1)
    body0 = _candle_body(c0)

    # ── Momentum shift: current candle must be bullish with expanding range ──
    if not _is_bullish(c0):
        return False, ""
    if rng0 < rng1 * 0.8:   # range must not be shrinking vs prior bar
        return False, ""

    # ── Volatility confirmation: current range >= 0.8× ATR ──────────────────
    if rng0 < atr_v * 0.8:
        return False, ""

    # ── Pattern 1: Bullish Engulfing ─────────────────────────────────────────
    if (_is_bearish(c1) and
            c0["close"] > c1["open"] and
            c0["open"]  < c1["close"] and
            _body_ratio(c0) >= 0.55):
        return True, "ENGULF_BULL"

    # ── Pattern 2: Hammer (lower wick >= 2× body, small upper wick) ─────────
    lw0 = _lower_wick(c0)
    uw0 = _upper_wick(c0)
    if (body0 > 0 and
            lw0 >= 2.0 * body0 and
            uw0 <= body0 * 0.5 and
            _body_ratio(c0) >= 0.2):
        return True, "HAMMER"

    # ── Pattern 3: Bullish swing — current low > prev low (HLow forming) ────
    if (_is_bullish(c0) and
            c0["low"] > c1["low"] and
            c1["low"] < c2["low"] and   # c1 was the swing low pivot
            _body_ratio(c0) >= 0.45):
        return True, "SWING_HL"

    # ── Pattern 4: Morning-star style (3-bar) ────────────────────────────────
    if (_is_bearish(c2) and
            _body_ratio(c1) <= 0.35 and   # indecision doji/small body
            _is_bullish(c0) and
            c0["close"] > (c2["open"] + c2["close"]) / 2 and
            _body_ratio(c0) >= 0.45):
        return True, "MORNING_STAR"

    return False, ""


def confirm_reversal_bear(bars, atr_v):
    """
    Confirm a bearish reversal at upper/upper-middle band.
    Requires BOTH:
      1. Momentum shift: current candle shows bearish body and ATR expanding
      2. Pattern: bearish engulfing OR shooting star OR swing lower high formed
    Returns (confirmed: bool, pattern_name: str)
    """
    if len(bars) < 3:
        return False, ""
    c0 = bars[-1]
    c1 = bars[-2]
    c2 = bars[-3]

    rng0  = _candle_range(c0)
    rng1  = _candle_range(c1)
    body0 = _candle_body(c0)

    # ── Momentum shift: current candle must be bearish with expanding range ──
    if not _is_bearish(c0):
        return False, ""
    if rng0 < rng1 * 0.8:
        return False, ""

    # ── Volatility confirmation: current range >= 0.8× ATR ──────────────────
    if rng0 < atr_v * 0.8:
        return False, ""

    # ── Pattern 1: Bearish Engulfing ─────────────────────────────────────────
    if (_is_bullish(c1) and
            c0["close"] < c1["open"] and
            c0["open"]  > c1["close"] and
            _body_ratio(c0) >= 0.55):
        return True, "ENGULF_BEAR"

    # ── Pattern 2: Shooting Star (upper wick >= 2× body, small lower wick) ──
    uw0 = _upper_wick(c0)
    lw0 = _lower_wick(c0)
    if (body0 > 0 and
            uw0 >= 2.0 * body0 and
            lw0 <= body0 * 0.5 and
            _body_ratio(c0) >= 0.2):
        return True, "SHOOT_STAR"

    # ── Pattern 3: Bearish swing — current high < prev high (LHigh forming) ─
    if (_is_bearish(c0) and
            c0["high"] < c1["high"] and
            c1["high"] > c2["high"] and   # c1 was the swing high pivot
            _body_ratio(c0) >= 0.45):
        return True, "SWING_LH"

    # ── Pattern 4: Evening-star style (3-bar) ────────────────────────────────
    if (_is_bullish(c2) and
            _body_ratio(c1) <= 0.35 and
            _is_bearish(c0) and
            c0["close"] < (c2["open"] + c2["close"]) / 2 and
            _body_ratio(c0) >= 0.45):
        return True, "EVENING_STAR"

    return False, ""


def confirm_continuation_bull(bars, atr_v):
    """
    Confirm a bullish continuation at middle band / near swing low in uptrend.
    Requires momentum and volatility pushing upward with continuation structure.
    Returns (confirmed: bool, pattern_name: str)
    """
    if len(bars) < 3:
        return False, ""
    c0 = bars[-1]
    c1 = bars[-2]
    c2 = bars[-3]

    rng0 = _candle_range(c0)

    # ── Momentum: current bar bullish ────────────────────────────────────────
    if not _is_bullish(c0):
        return False, ""

    # ── Volatility: expanding range toward continuation direction ────────────
    if rng0 < atr_v * 0.7:
        return False, ""

    # ── Pattern 1: Consecutive bullish bars (momentum continuation) ──────────
    if (_is_bullish(c1) and
            c0["close"] > c1["close"] and
            _body_ratio(c0) >= 0.5 and
            _body_ratio(c1) >= 0.4):
        return True, "BULL_CONSEC"

    # ── Pattern 2: Pullback candle followed by strong resumption ─────────────
    if (_is_bearish(c1) and                  # pullback candle
            _body_ratio(c1) <= 0.5 and       # weak / shallow pullback
            c1["low"] > c2["low"] and        # higher low held
            _is_bullish(c0) and
            c0["close"] > c1["high"] and     # current breaks above pullback high
            _body_ratio(c0) >= 0.5):
        return True, "PULLBACK_BULL"

    # ── Pattern 3: Inside bar breakout upward ────────────────────────────────
    if (c1["high"] <= c2["high"] and         # inside bar
            c1["low"]  >= c2["low"] and
            c0["close"] > c2["high"] and     # breakout above mother bar
            _is_bullish(c0) and
            _body_ratio(c0) >= 0.5):
        return True, "IB_BREAK_BULL"

    return False, ""


def confirm_continuation_bear(bars, atr_v):
    """
    Confirm a bearish continuation at middle band / near swing high in downtrend.
    Returns (confirmed: bool, pattern_name: str)
    """
    if len(bars) < 3:
        return False, ""
    c0 = bars[-1]
    c1 = bars[-2]
    c2 = bars[-3]

    rng0 = _candle_range(c0)

    # ── Momentum: current bar bearish ────────────────────────────────────────
    if not _is_bearish(c0):
        return False, ""

    # ── Volatility: expanding range toward continuation direction ────────────
    if rng0 < atr_v * 0.7:
        return False, ""

    # ── Pattern 1: Consecutive bearish bars ──────────────────────────────────
    if (_is_bearish(c1) and
            c0["close"] < c1["close"] and
            _body_ratio(c0) >= 0.5 and
            _body_ratio(c1) >= 0.4):
        return True, "BEAR_CONSEC"

    # ── Pattern 2: Pullback candle followed by strong resumption downward ────
    if (_is_bullish(c1) and               # pullback candle
            _body_ratio(c1) <= 0.5 and    # weak / shallow pullback
            c1["high"] < c2["high"] and   # lower high held
            _is_bearish(c0) and
            c0["close"] < c1["low"] and   # current breaks below pullback low
            _body_ratio(c0) >= 0.5):
        return True, "PULLBACK_BEAR"

    # ── Pattern 3: Inside bar breakout downward ───────────────────────────────
    if (c1["high"] <= c2["high"] and      # inside bar
            c1["low"]  >= c2["low"] and
            c0["close"] < c2["low"] and   # breakout below mother bar
            _is_bearish(c0) and
            _body_ratio(c0) >= 0.5):
        return True, "IB_BREAK_BEAR"

    return False, ""


def analyse(sym):
    bars = list(S["candles"].get(sym, []))
    empty = {"struct":"RANGING","signal":None,"setup":"","reason":f"{len(bars)}/20 bars",
             "confirm_pat":"",
             "trend":"─","bb_zone":"─","ema30":0,"exhaustion":0,
             "near_sl":False,"near_sh":False,"last_close":0,"candle_count":len(bars)}
    if len(bars) < 20: return empty

    closes = [b["close"] for b in bars]
    e21    = ema(closes, EMA_FAST)
    e30    = ema(closes, EMA_TREND)
    bu, bm, bl = bbands(closes)
    atr_v  = calc_atr(bars)[-1]
    curr   = bars[-1]["close"]

    # Trend
    rising  = e30[-1]>e30[-3] if len(e30)>=3 else False
    falling = e30[-1]<e30[-3] if len(e30)>=3 else False
    t_up    = curr>e30[-1] and rising
    t_dn    = curr<e30[-1] and falling
    trend   = "UP" if t_up else "DOWN" if t_dn else "FLAT"

    # BB zone
    tol = atr_v*0.4
    if   abs(curr-bm[-1])<tol:  zone="MIDDLE"
    elif abs(curr-bu[-1])<tol:  zone="UPPER"
    elif abs(curr-bl[-1])<tol:  zone="LOWER"
    elif curr>bm[-1]:            zone="ABOVE_MID"
    else:                        zone="BELOW_MID"

    # Swings + progressive BOS/CHoCH structure
    hi, lo  = swings(bars)
    sres    = update_structure(sym, bars, hi, lo)
    struct  = sres["struct"]
    bos     = sres["bos"]
    choch   = sres["choch"]
    ex      = exh_score(bars, e21)
    t       = atr_v*1.0
    nsl     = bool(lo and abs(curr-lo[-1]["p"])<t)
    nsh     = bool(hi and abs(curr-hi[-1]["p"])<t)
    ll      = lo[-1]["p"] if lo else curr
    lh      = hi[-1]["p"] if hi else curr

    # Signal — struct now includes CHOCH_BULL / CHOCH_BEAR for early reversals
    # All signals now require candle-pattern + momentum/volatility confirmation
    # before entry.  Reversal zones require engulfing / hammer / shooting-star /
    # swing-formation.  Continuation zones require consecutive momentum bars,
    # pullback-resumption, or inside-bar breakout.
    sig, setup, reason, direction = None, "", "", None
    confirm_pat = ""
    bull_ok = struct in ("BULLISH", "CHOCH_BULL", "RANGING")
    bear_ok = struct in ("BEARISH", "CHOCH_BEAR", "RANGING")
    if ex < 65:
        # Bullish continuation: uptrend at middle band, bouncing off swing low
        if t_up and zone in ("MIDDLE","BELOW_MID") and nsl and bull_ok:
            ok, confirm_pat = confirm_continuation_bull(bars, atr_v)
            if ok:
                tag = "BOS" if bos else ("CHOCH" if choch else "CONT")
                sig, setup, reason = "RISE", tag, f"UP·MidBB·SwLow@{ll:.5f}·{confirm_pat}"
                direction = "CALL"
        # Bullish reversal: downtrend touching lower band
        elif t_dn and zone in ("LOWER","BELOW_MID") and nsl and bull_ok:
            ok, confirm_pat = confirm_reversal_bull(bars, atr_v)
            if ok:
                tag = "CHOCH" if choch else "REV"
                sig, setup, reason = "RISE", tag, f"DN·LowBB·SwLow@{ll:.5f}·{confirm_pat}"
                direction = "CALL"
        # Bearish continuation: downtrend at middle band, rejecting swing high
        elif t_dn and zone in ("MIDDLE","ABOVE_MID") and nsh and bear_ok:
            ok, confirm_pat = confirm_continuation_bear(bars, atr_v)
            if ok:
                tag = "BOS" if bos else ("CHOCH" if choch else "CONT")
                sig, setup, reason = "FALL", tag, f"DN·MidBB·SwHi@{lh:.5f}·{confirm_pat}"
                direction = "PUT"
        # Bearish reversal: uptrend touching upper band
        elif t_up and zone in ("UPPER","ABOVE_MID") and nsh and bear_ok:
            ok, confirm_pat = confirm_reversal_bear(bars, atr_v)
            if ok:
                tag = "CHOCH" if choch else "REV"
                sig, setup, reason = "FALL", tag, f"UP·UpBB·SwHi@{lh:.5f}·{confirm_pat}"
                direction = "PUT"

    return {"struct":struct,"signal":sig,"setup":setup,"reason":reason,
            "direction":direction,"confirm_pat":confirm_pat,
            "trend":trend,"bb_zone":zone,"ema30":round(e30[-1],4),
            "exhaustion":ex,"near_sl":nsl,"near_sh":nsh,
            "bos":bos,"choch":choch,
            "last_close":curr,"candle_count":len(bars)}

# ── WebSocket ─────────────────────────────────────────────────────────────────
async def ws_connect():
    ctx = ssl.create_default_context()
    reader, writer = await asyncio.open_connection(WS_HOST, 443, ssl=ctx)
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    writer.write((
        f"GET {WS_PATH} HTTP/1.1\r\nHost: {WS_HOST}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    await writer.drain()
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += await reader.read(1024)
    return reader, writer

def ws_frame(data, op=1):
    mask = os.urandom(4)
    pay  = bytes(b^mask[i%4] for i,b in enumerate(data))
    n    = len(data)
    if n<126:   hdr = bytes([0x80|op, 0x80|n])
    elif n<65536: hdr = bytes([0x80|op,0x80|126,(n>>8)&0xFF,n&0xFF])
    else:         hdr = bytes([0x80|op,0x80|127]+[(n>>(8*(7-i)))&0xFF for i in range(8)])
    return hdr+mask+pay

async def ws_read_frame(reader):
    h  = await reader.readexactly(2)
    op = h[0]&0x0F
    n  = h[1]&0x7F
    if n==126: n=int.from_bytes(await reader.readexactly(2),'big')
    elif n==127: n=int.from_bytes(await reader.readexactly(8),'big')
    mk = await reader.readexactly(4) if h[1]&0x80 else b''
    pay= await reader.readexactly(n)
    if mk: pay = bytes(b^mk[i%4] for i,b in enumerate(pay))
    return op, pay

async def send(msg):
    ws = S["ws"]
    if not ws or ws.get("closed"): return None
    S["req_id"] += 1
    msg["req_id"] = S["req_id"]
    frame = ws_frame(json.dumps(msg).encode())
    ws["writer"].write(frame)
    await ws["writer"].drain()
    return S["req_id"]

# ── API helpers ───────────────────────────────────────────────────────────────
async def authorize():
    await send({"authorize": S["token"]})

async def fetch_candles(sym):
    await send({"ticks_history":sym,"granularity":CANDLE_TF,
                "count":CANDLE_COUNT,"end":"latest","style":"candles",
                "adjust_start_time":1})

async def subscribe_ohlc(sym):
    await send({"ticks_history":sym,"granularity":CANDLE_TF,
                "count":1,"end":"latest","style":"candles","subscribe":1})

# ── Symbol state helper (mirrors get_symbol_state) ───────────────────────────
def _sym_state(sym):
    if sym not in S["symbol_state"]:
        S["symbol_state"][sym] = {"pending_fps": []}
    return S["symbol_state"][sym]

# ── send_proposal — exact mirror of reference implementation ─────────────────
async def place_trade(sym, sig, direction, stake, setup_key="", dedup_key="", entry_candles=None):
    """
    Mirrors send_proposal(ws, symbol, direction, fingerprint):
      1. Build fingerprint
      2. Append to symbol pending_fps BEFORE sending
      3. Send proposal request
    direction must be "CALL" or "PUT"
    setup_key:     pattern key (sig|setup|struct) — used for cross-symbol memory lookup
    dedup_key:     runtime key (sym|sig|setup|struct) — used to release active_setup_keys lock
    entry_candles: last 10 candle dicts at the moment of signal (saved to trade memory)
    """
    state = _sym_state(sym)

    fingerprint = {
        "symbol":        sym,
        "direction":     direction,
        "sig":           sig,
        "stake":         stake,
        "setup_key":     setup_key,
        "dedup_key":     dedup_key,
        "timestamp":     time.time(),
        "entry_candles": entry_candles or [],
    }
    # Store fingerprint BEFORE sending proposal — exactly as reference does
    state["pending_fps"].append(fingerprint)
    log_file("trade", f"send_proposal {direction} {sym} ${stake} fp_queued={len(state['pending_fps'])}")

    rid = await send({
        "proposal":      1,
        "amount":        stake,
        "basis":         "stake",
        "contract_type": direction,
        "currency":      S["currency"],
        "duration":      CONTRACT_DUR,
        "duration_unit": "m",
        "symbol":        sym,
    })
    if rid:
        log(f"Proposal sent {direction} {sym} ${stake}", "trade")
    else:
        log_file("error", f"place_trade {sym}: send() returned no req_id — WS disconnected?")
        log(f"Proposal send failed {sym}", "error")

# ── Message handler ───────────────────────────────────────────────────────────
async def on_message(msg):
    try:
        await _on_message(msg)
    except Exception as exc:
        import traceback
        log_file("error", f"on_message unhandled exception: {exc}\n{traceback.format_exc()}")
        log(f"Handler crash: {exc}", "error")

async def _on_message(msg):
    if msg.get("error"):
        e = msg["error"]
        log_file("api_err", f"msg_type={msg.get('msg_type','?')} code={e.get('code','?')} msg={e.get('message','?')} full={msg}")
        log(f"API {msg.get('msg_type','?')} ERR {e.get('code','?')}: {e.get('message','?')}", "error")
        return

    t = msg.get("msg_type")

    if t == "authorize":
        a = msg["authorize"]
        S.update({"authorized":True,"balance":a.get("balance",0),
                  "currency":a.get("currency","USD"),
                  "account_type":a.get("account_type","")})
        log(f"Auth OK · {a.get('email','')} · {S['account_type'].upper()}", "ok")
        # Subscribe to balance updates — stake/TP/SL applied when start cmd arrives
        await send({"balance":1,"subscribe":1})
        # Begin loading candle history immediately so data is ready when bot starts
        for sym in S["active_syms"]:
            await fetch_candles(sym)

    elif t == "balance":
        S["balance"] = msg["balance"]["balance"]
        # In auto mode, recalculate stake/TP/SL whenever balance updates
        if S["stake_mode"] == "auto":
            apply_auto_stake(S["balance"])

    elif t == "candles":
        req = msg.get("echo_req",{})
        sym = req.get("ticks_history","")
        if req.get("subscribe")==1:
            log(f"{sym} stream live", "ok")
            return
        raw = msg.get("candles",[])
        dq  = deque(maxlen=CANDLE_COUNT+10)
        for c in raw:
            dq.append({"time":int(c["epoch"]),"open":float(c["open"]),
                       "high":float(c["high"]),"low":float(c["low"]),"close":float(c["close"])})
        S["candles"][sym] = dq
        # Reset progressive structure state so it rebuilds from fresh candle history
        _sym_state(sym).pop("struct_state", None)
        log(f"{sym}: {len(dq)} candles loaded", "ok")
        S["analysis"][sym] = analyse(sym)
        await subscribe_ohlc(sym)

    elif t == "ohlc":
        o   = msg["ohlc"]
        sym = o["symbol"]
        if sym not in S["candles"]: return
        dq  = S["candles"][sym]
        bar = {"time":int(o["open_time"]),"open":float(o["open"]),
               "high":float(o["high"]),"low":float(o["low"]),"close":float(o["close"])}
        if dq and dq[-1]["time"]==bar["time"]: dq[-1]=bar
        else: dq.append(bar)
        S["last_tick"][sym] = bar["close"]
        a = analyse(sym)
        S["analysis"][sym] = a
        # Always call check_signal — it logs internally why trades are skipped
        await check_signal(sym, a)

    elif t == "proposal":
        # ── handle_proposal (reference implementation, async) ─────────────────
        p       = msg.get("proposal", {})
        prop_id = str(p.get("id", ""))

        # echo_req holds the original request fields (symbol, contract_type, etc.)
        echo = msg.get("echo_req") or p.get("echo_req") or {}

        # 1. Store proposal metadata keyed by proposal_id
        S["proposals"][prop_id] = echo

        sym   = echo.get("symbol", "?")
        stake = float(echo.get("amount", p.get("ask_price", 0)))
        ct    = echo.get("contract_type", "?")

        state    = _sym_state(sym)
        fps_list = state["pending_fps"]

        # 2. Link latest fingerprint to this proposal_id (don't pop yet)
        if fps_list:
            S["proposal_fps"][prop_id] = fps_list[-1]

        log_file("trade", f"handle_proposal pid={prop_id} sym={sym} ct={ct} fps_pending={len(fps_list)}")

        # 3. AUTO BUY IMMEDIATELY — use ask_price from proposal
        if prop_id:
            ask = float(p.get("ask_price", stake))
            await send({"buy": prop_id, "price": ask})
            log(f"Buy {ct} {sym} ${ask}", "trade")
        else:
            log_file("error", f"handle_proposal: empty proposal_id sym={sym} raw={msg}")
            log(f"Proposal missing id sym={sym}", "error")

    elif t == "buy":
        # ── handle_buy (reference implementation, async) ──────────────────────
        b        = msg.get("buy", {})
        echo_req = msg.get("echo_req", {})

        contract_id = b.get("contract_id")
        prop_id     = str(echo_req.get("buy", ""))

        # 1. Get proposal metadata stored during handle_proposal
        prop_echo = S["proposals"].get(prop_id, {})
        sym       = prop_echo.get("symbol", "?")
        direction = prop_echo.get("contract_type", "?")   # "CALL" or "PUT"
        trade_type = "RISE" if direction == "CALL" else "FALL"

        state = _sym_state(sym)

        # 2. Resolve fingerprint — primary by proposal_id, fallback FIFO
        fp = None
        if prop_id in S["proposal_fps"]:
            fp = S["proposal_fps"].pop(prop_id)
            fps_list = state["pending_fps"]
            if fp in fps_list:
                fps_list.remove(fp)
        else:
            fps_list = state["pending_fps"]
            if fps_list:
                fp = fps_list.pop(0)   # FIFO fallback
            log_file("warn", f"handle_buy {sym}: prop_id {prop_id} not in proposal_fps, used FIFO fallback")

        # 3. Final mapping contract_id → fingerprint
        if contract_id and fp:
            S["pending_fingerprints"][contract_id] = fp

        log_file("trade", f"handle_buy cid={contract_id} sym={sym} dir={direction} fp={'ok' if fp else 'MISSING'}")

        if contract_id:
            S["open_contracts"][contract_id] = {
                "sym":   sym,
                "type":  trade_type,
                "stake": b.get("buy_price", 0),
                "fp":    fp,
            }
            # Record contract_id → sym so the settlement handler can release
            # the correct symbol's slot counter when the contract closes.
            S["cid_to_sym"][contract_id] = sym
            await send({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1})
            log(f"Contract #{contract_id} {trade_type} {sym} open · slots={_open_count_for_sym(sym)}/{S['max_trades_per_sym']}", "ok")
        else:
            # Buy failed — release the slot we claimed when we sent the proposal
            # We don't know which slot failed so release one slot from this sym
            # (sym was resolved from proposals dict above)
            if sym and sym != "?":
                _release_slot(sym)
                log_file("warn", f"handle_buy: no contract_id, released slot for {sym}")
            log_file("error", f"handle_buy: no contract_id prop_id={prop_id} sym={sym} raw={msg}")
            log(f"Buy response missing contract_id sym={sym}", "error")

    elif t == "proposal_open_contract":
        poc = msg.get("proposal_open_contract", {})
        cid = poc.get("contract_id")

        # ── Live contract update (not yet sold) ───────────────────────────────
        if cid and not poc.get("is_sold"):
            info = S["open_contracts"].get(cid, {})
            current_spot = float(poc.get("current_spot", 0))
            entry_spot   = float(poc.get("entry_spot",   0))
            bid          = float(poc.get("bid_price",    0))
            buy_price    = float(poc.get("buy_price",    info.get("stake", 0)))
            live_pnl     = bid - buy_price
            # Update live tracking record
            S["contract_history"].setdefault(cid, {
                "cid":       cid,
                "sym":       info.get("sym", "?"),
                "type":      info.get("type", "?"),
                "setup_key": info.get("fp", {}).get("setup_key", "") if isinstance(info.get("fp"), dict) else "",
                "stake":     info.get("stake", 0),
                "entry":     entry_spot,
                "open_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "updates":   [],
            })
            S["contract_history"][cid]["current_spot"] = current_spot
            S["contract_history"][cid]["live_pnl"]     = live_pnl
            S["contract_history"][cid]["updates"].append({
                "t": datetime.now().strftime("%H:%M:%S"),
                "spot": current_spot,
                "pnl":  round(live_pnl, 4),
            })

        # ── Contract settled ──────────────────────────────────────────────────
        if poc.get("is_sold") and cid:
            pnl  = float(poc.get("profit", 0))
            info = S["open_contracts"].pop(cid, {})
            fp        = info.get("fp") or {}
            setup_key = fp.get("setup_key", "") if isinstance(fp, dict) else ""
            dedup_key = fp.get("dedup_key", setup_key) if isinstance(fp, dict) else setup_key

            # Release dedup lock (per-symbol runtime key)
            S["active_setup_keys"].discard(dedup_key)

            # Release the slot for this symbol using the contract_id → sym map
            # This is the authoritative release point — the slot was claimed when
            # the proposal was sent and is now freed because the contract is closed.
            settled_sym = S["cid_to_sym"].pop(cid, info.get("sym", ""))
            if settled_sym:
                _release_slot(settled_sym)
                log_file("trade", f"slot released {settled_sym} → slots now={_open_count_for_sym(settled_sym)}")

            S["pnl"] += pnl
            outcome = "win" if pnl >= 0 else "loss"

            # Build trade memory record — pattern-focused, no prices/timestamps/session data
            record = {
                "setup_key":     setup_key,          # sig|setup|struct (cross-symbol pattern)
                "sym":           info.get("sym", "?"),
                "sig":           fp.get("sig",       "?") if isinstance(fp, dict) else "?",
                "direction":     fp.get("direction", "?") if isinstance(fp, dict) else "?",
                "type":          info.get("type", "?"),
                "outcome":       outcome,
                "entry_candles": fp.get("entry_candles", []) if isinstance(fp, dict) else [],
            }
            S["contract_history"][cid].update(record)

            # Save to win/loss JSON (no duplicate setup keys per file)
            _save_trade(outcome, record)

            # Session trades list (for dashboard right panel)
            S["trades"].append({
                "cid":   cid,
                "pnl":   pnl,
                "sym":   info.get("sym", "?"),
                "type":  info.get("type", "?"),
                "setup": setup_key.split("|")[1] if "|" in setup_key else "",
                "time":  datetime.now().strftime("%H:%M:%S"),
            })

            if pnl >= 0:
                S["wins"] += 1; S["streak"] = 0
                log(f"WIN #{cid} {info.get('sym','?')} +${pnl:.2f} [{setup_key}]", "win")
                risk_check()   # check TP after every win
            else:
                S["losses"] += 1; S["streak"] += 1
                log(f"LOSS #{cid} {info.get('sym','?')} -${abs(pnl):.2f} streak={S['streak']} [{setup_key}]", "loss")
                risk_check()

# ── Concurrency helpers ───────────────────────────────────────────────────────
def _open_count_for_sym(sym):
    """
    Return the number of claimed slots for this symbol.
    Uses sym_open_slots which is incremented the moment a proposal is sent
    (before Deriv sends back the buy confirmation) and decremented when the
    contract settles.  This is more accurate than counting open_contracts
    because open_contracts is only populated after the buy response arrives,
    meaning two proposals sent back-to-back would both pass the slot check.
    """
    return S["sym_open_slots"].get(sym, 0)

def _claim_slot(sym):
    """Increment the open-slot counter for sym. Called just before sending proposal."""
    S["sym_open_slots"][sym] = S["sym_open_slots"].get(sym, 0) + 1

def _release_slot(sym):
    """Decrement the open-slot counter for sym. Called when contract settles."""
    current = S["sym_open_slots"].get(sym, 0)
    S["sym_open_slots"][sym] = max(0, current - 1)

def _syms_with_open_trades():
    """Return the set of symbols that currently have at least one claimed slot."""
    return {sym for sym, cnt in S["sym_open_slots"].items() if cnt > 0}

async def check_signal(sym, a):
    # ── Gate checks — all logged so nothing is silent ──────────────────────────
    if not S["running"]:
        reason = S["stopped_reason"] or "not started"
        log_file("skip", f"{sym}: bot not running — {reason}")
        return
    if S["paused"]:
        log_file("skip", f"{sym}: bot paused")
        return
    if S["stopped_reason"]:
        log_file("skip", f"{sym}: stopped — {S['stopped_reason']}")
        return

    sig       = a.get("signal")
    direction = a.get("direction")
    if not sig or not direction:
        log_file("skip", f"{sym}: no signal — ex={a.get('exhaustion','?')} trend={a.get('trend','?')} zone={a.get('bb_zone','?')} struct={a.get('struct','?')} bars={a.get('candle_count','?')}")
        return

    last = S["signals"].get(sym, {})
    bars = S["candles"].get(sym, [])
    if not bars:
        log_file("skip", f"{sym}: no candle bars")
        return
    ct = bars[-1]["time"]
    if last.get("sig") == sig and last.get("ct") == ct:
        log_file("skip", f"{sym}: duplicate {sig} on candle {ct}, waiting for new bar")
        return

    stake = S["stakes"].get(sym, 0)
    if stake <= 0:
        log_file("skip", f"{sym}: stake={stake}")
        log(f"No stake set for {sym}", "warn")
        return

    setup     = a.get("setup", "")
    struct    = a.get("struct", "")
    setup_key = _setup_key(sym, sig, setup, struct)
    dedup_key = _dedup_key(sym, setup_key)

    # ── Max symbols trading at once ───────────────────────────────────────────
    active_syms_set = _syms_with_open_trades()
    max_syms = S["max_syms_trading"]
    if sym not in active_syms_set and len(active_syms_set) >= max_syms:
        log_file("skip", f"{sym}: max_syms_trading={max_syms} already reached ({active_syms_set})")
        return

    # ── Max open trades per symbol ────────────────────────────────────────────
    max_per_sym  = S["max_trades_per_sym"]
    current_open = _open_count_for_sym(sym)
    slots_open   = max_per_sym - current_open   # how many more we can open now
    if slots_open <= 0:
        log_file("skip", f"{sym}: max_trades_per_sym={max_per_sym} already open ({current_open})")
        return

    # ── Trade memory: read history, block if setup is persistently losing ────
    skip, mem_reason = _should_skip_on_memory(setup_key)
    w, l, last_outcome, _ = _memory_summary(setup_key)
    total = w + l
    wr_pct   = int(w / total * 100) if total > 0 else 0
    mem_note = f"mem={w}W/{l}L({wr_pct}%) last={last_outcome}" if total > 0 else "mem=new"
    log_file("memory", f"{sym} {setup_key}: {mem_reason}")

    if skip:
        log(f"SKIP {sym} {sig}·{setup} — {mem_reason}", "warn")
        return

    # Snapshot the last 10 candles at the moment of entry
    bars_list     = list(bars)
    entry_candles = bars_list[-10:] if len(bars_list) >= 10 else bars_list[:]

    S["signals"][sym] = {"sig": sig, "ct": ct}
    cpat = a.get("confirm_pat", "")
    log(f"SIGNAL {sig}({direction})·{setup} {sym} {a.get('reason','')} pat={cpat} slots={slots_open}/{max_per_sym} {mem_note}", "signal")

    # ── Fire one proposal per available slot ──────────────────────────────────
    # We claim each slot BEFORE sending the proposal so that even if multiple
    # ohlc ticks arrive before the Deriv buy-response comes back, subsequent
    # check_signal calls see accurate slot counts and do not over-fire.
    for slot in range(slots_open):
        slot_dedup = f"{dedup_key}|slot{slot}"
        S["active_setup_keys"].add(slot_dedup)
        _claim_slot(sym)   # reserve this slot immediately
        await place_trade(sym, sig, direction, stake, setup_key, slot_dedup, entry_candles)

def risk_check():
    # Loss streak guard
    if S["streak"] >= MAX_STREAK:
        S["running"] = False
        S["stopped_reason"] = f"Loss streak {S['streak']}"
        log(f"STOPPED: {S['stopped_reason']}", "error")
        return
    # Session SL hit
    if S["session_sl"] > 0 and S["pnl"] <= -S["session_sl"]:
        S["running"] = False
        S["stopped_reason"] = f"SL hit (${S['pnl']:.2f} / -${S['session_sl']:.2f})"
        log(f"STOPPED: {S['stopped_reason']}", "error")
        return
    # Session TP hit (checked after every win too — called from on_message)
    if S["session_tp"] > 0 and S["pnl"] >= S["session_tp"]:
        S["running"] = False
        S["stopped_reason"] = f"TP hit (${S['pnl']:.2f} / +${S['session_tp']:.2f})"
        log(f"STOPPED: {S['stopped_reason']}", "ok")

def apply_auto_stake(balance):
    """Compute TP, SL, and per-trade stake from live account balance."""
    S["session_tp"] = round(balance * 0.10, 2)
    S["session_sl"] = round(balance * 0.05, 2)
    # Stake = SL / 4  →  4 consecutive losses exhaust the daily SL
    stake = round(S["session_sl"] / 4, 2)
    stake = max(stake, 0.35)   # Deriv minimum stake floor
    S["default_stake"] = stake
    for sym in S["active_syms"]:
        S["stakes"][sym] = stake
    log(f"AUTO · Bal:${balance:.2f} · TP:${S['session_tp']:.2f} · SL:${S['session_sl']:.2f} · Stake:${stake:.2f}", "ok")


# ── Web-mode logger ──────────────────────────────────────────────────────────
def log_print(msg, kind="info"):
    """
    Append to the in-memory log ring-buffer and persist to errors.log.
    Does NOT write directly to stdout — the dashboard_loop redraws the
    log section every DASH_INTERVAL seconds inside the live dashboard.
    For urgent events (win/loss/signal/error) we flush the dashboard
    immediately by writing a quick redraw trigger.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    c = {"info":WHT,"ok":GRN,"error":RED,"trade":CYN,
         "win":GRN,"loss":RED,"signal":YLW,"warn":ORG}.get(kind, WHT)
    i = {"info":"·","ok":"✓","error":"✗","trade":"◆",
         "win":"▲","loss":"▼","signal":"⚡","warn":"⚠"}.get(kind, "·")
    line = f"{DIM}{ts}{R} {c}{i} {msg}{R}"
    S["log"].appendleft(line)
    log_file(kind, msg)

    # For high-priority events push an immediate dashboard refresh
    if kind in ("win", "loss", "signal", "error", "trade"):
        try:
            dash = _build_dashboard()
            sys.stdout.write("\033[H" + dash + "\033[J")
            sys.stdout.flush()
        except Exception:
            pass  # never crash the logger

# Monkey-patch log → log_print so all existing log() calls use this
log = log_print

# ── Dashboard renderer ───────────────────────────────────────────────────────
# xterm.js understands ANSI codes — we use [H to home the cursor and
# [J to erase below, giving a full redraw every DASH_INTERVAL seconds.
DASH_INTERVAL = 5   # seconds between full redraws

def _build_dashboard():
    """Build the full dashboard string for xterm output."""
    W = 120  # fixed width — matches xterm default cols
    out = []

    def hl(): return f"{DIM}{'─' * W}{R}"
    def ln(s=""): out.append(s)
    def pad(s, w, colour=WHT):
        visible = ANSI_RE.sub("", s)
        trunc   = visible[:w]
        return f"{colour}{trunc}{R}{' ' * (w - len(trunc))}"

    # ── Header ────────────────────────────────────────────────────────────────
    conn_s = f"{GRN}LIVE{R}"    if S["connected"]  else f"{RED}OFFLINE{R}"
    auth_s = f"{GRN}AUTH{R}"    if S["authorized"] else f"{YLW}PENDING{R}"
    run_s  = (f"{GRN}RUNNING{R}" if S["running"] else f"{RED}STOPPED{R}")
    bal_s  = f"{GRN}${S['balance']:.2f}{R} {DIM}{S['currency']}{R}"
    mode_c = BLU if S["stake_mode"] == "auto" else CYN
    mode_s = f"{mode_c}{S['stake_mode'].upper()}{R}"
    tp_s   = f"{GRN}TP:${S['session_tp']:.2f}{R}" if S["session_tp"] > 0 else f"{DIM}TP:OFF{R}"
    sl_s   = f"{RED}SL:${S['session_sl']:.2f}{R}" if S["session_sl"] > 0 else f"{DIM}SL:OFF{R}"
    acc_s  = f" {BLU}{S['account_type'].upper()}{R}" if S["account_type"] else ""
    ln(f"{BLD}{WHT} DERIV BOT{R}  {conn_s}  {auth_s}  {run_s}  Bal:{bal_s}{acc_s}  {mode_s}  {tp_s}  {sl_s}")
    ln(hl())

    # ── Symbol table header ───────────────────────────────────────────────────
    # cols: SYM(10) PRICE(11) TREND(6) BB-ZONE(10) EMA30(10) STRUCT(11) EXHST(6) STAKE(8) SIGNAL(12) REASON(rest)
    CW = [10, 11, 6, 10, 10, 11, 6, 8, 12, 0]
    CW[-1] = max(8, W - sum(CW[:-1]) - 2)
    hdrs   = ["SYMBOL", "PRICE", "TREND", "BB-ZONE", "EMA30", "STRUCT", "EXHST", "STAKE", "SIGNAL", "REASON"]
    hrow   = " " + "".join(pad(h, CW[i], CYN) for i, h in enumerate(hdrs))
    ln(hrow)
    ln(hl())

    # ── Per-symbol rows ───────────────────────────────────────────────────────
    for sym in S["active_syms"]:
        a      = S["analysis"].get(sym, {})
        tick   = S["last_tick"].get(sym, a.get("last_close", 0))
        stake  = S["stakes"].get(sym, 0)
        trend  = a.get("trend", "─")
        zone   = a.get("bb_zone", "─")
        ex     = a.get("exhaustion", 0)
        sig    = a.get("signal", "")
        setup  = a.get("setup", "")
        struct = a.get("struct", "─")
        reason = a.get("reason", "─")

        tc = GRN if trend == "UP" else RED if trend == "DOWN" else DIM
        zc = GRN if zone == "LOWER" else RED if zone == "UPPER" else YLW if zone == "MIDDLE" else DIM
        sc = GRN if sig == "RISE" else RED if sig == "FALL" else DIM
        ec = RED if ex > 70 else YLW if ex > 45 else GRN
        mc = GRN if struct in ("BULLISH","CHOCH_BULL") else RED if struct in ("BEARISH","CHOCH_BEAR") else DIM
        nmark   = "▲" if a.get("near_sl") else "▼" if a.get("near_sh") else " "
        sigstr  = f"{sig}·{setup}" if sig and setup else sig or "─"
        sigdisp = f"{nmark}{sigstr}"

        row = (" "
               + pad(sym, CW[0], WHT)
               + pad(f"{tick:.5f}", CW[1], YLW)
               + pad(trend, CW[2], tc)
               + pad(zone, CW[3], zc)
               + pad(f"{a.get('ema30',0):.4f}", CW[4], BLU)
               + pad(struct, CW[5], mc)
               + pad(str(ex), CW[6], ec)
               + pad(f"${stake:.2f}", CW[7], CYN)
               + pad(sigdisp, CW[8], sc)
               + pad(reason[:CW[9]], CW[9], DIM))
        ln(row)

    if not S["active_syms"]:
        ln(f" {DIM}No symbols loaded{R}")
    ln(hl())

    # ── Stats bar ─────────────────────────────────────────────────────────────
    tot    = S["wins"] + S["losses"]
    wr     = f"{S['wins'] / tot * 100:.0f}%" if tot else "─"
    pc     = GRN if S["pnl"] >= 0 else RED
    pnl_s  = f"{pc}${S['pnl']:+.2f}{R}"
    open_n = len(S["open_contracts"])
    syms_n = len(_syms_with_open_trades())
    sk_d   = f"  {DIM}Stake{R} {CYN}${S['default_stake']:.2f}{R}" if S["default_stake"] > 0 else ""
    conc   = f"  {DIM}Syms{R} {CYN}{syms_n}/{S['max_syms_trading']}{R}  {DIM}T/Sym{R} {CYN}{S['max_trades_per_sym']}{R}"
    ln(f" {DIM}W{R}{GRN}{S['wins']}{R}  {DIM}L{R}{RED}{S['losses']}{R}"
       f"  {DIM}WR{R} {WHT}{wr}{R}"
       f"  {DIM}PnL{R} {pnl_s}"
       f"  {DIM}Open{R} {CYN}{open_n}{R}"
       f"  {DIM}Streak{R} {RED if S['streak'] >= 2 else WHT}{S['streak']}{R}"
       + sk_d + conc
       + f"  {DIM}TF{R} {BLU}1m{R}  {DIM}Dur{R} {BLU}2m{R}")
    if S["stopped_reason"]:
        ln(f" {RED}{BLD}STOPPED: {S['stopped_reason']}{R}")
    ln(hl())

    # ── Open contracts ────────────────────────────────────────────────────────
    open_cids = list(S["open_contracts"].keys())
    if open_cids:
        ln(f" {CYN}OPEN CONTRACTS:{R}")
        for cid in open_cids[:6]:
            hist  = S["contract_history"].get(cid, {})
            info  = S["open_contracts"].get(cid, {})
            sym_m = (hist.get("sym") or info.get("sym", "?"))[:8]
            typ_m = hist.get("type") or info.get("type", "?")
            lpnl  = hist.get("live_pnl")
            sk_k  = hist.get("setup_key", "")
            setup_short = sk_k.split("|")[1] if "|" in sk_k else "─"
            tc_m  = GRN if typ_m == "RISE" else RED
            if lpnl is not None:
                pc_m = GRN if lpnl >= 0 else RED
                pnl_str = f"{pc_m}${lpnl:+.2f}{R}"
            else:
                pnl_str = f"{DIM}pending{R}"
            ln(f"   {DIM}#{cid}{R}  {WHT}{sym_m:<9}{R}  {tc_m}{typ_m:<5}{R}  {pnl_str}  {DIM}{setup_short}{R}")
        ln(hl())

    # ── Recent log lines ──────────────────────────────────────────────────────
    ln(f" {CYN}RECENT LOG:{R}")
    log_lines = list(S["log"])[:8]
    for ll in log_lines:
        # trim to fit terminal width
        visible = ANSI_RE.sub("", ll)
        if len(visible) > W - 1:
            ratio = (W - 1) / len(visible)
            ll = ll[:int(len(ll) * ratio)]
        ln(" " + ll)

    return "\r\n".join(out)


async def dashboard_loop():
    """Redraw the full dashboard in-place every DASH_INTERVAL seconds."""
    # Wait until authorized and running before drawing
    while not (S["authorized"] and S["running"]):
        await asyncio.sleep(1)

    # Initial clear
    sys.stdout.write("[2J[H")
    sys.stdout.flush()

    while True:
        dash = _build_dashboard()
        # Move cursor to top-left, write dashboard, erase from cursor to end
        sys.stdout.write("[H" + dash + "[J")
        sys.stdout.flush()
        await asyncio.sleep(DASH_INTERVAL)

# ── Command file watcher ───────────────────────────────────────────────────────
async def cmd_watcher():
    """
    Polls TBOT_CMD_FILE every second for commands written by front.py.
    Supported actions:
      start   — initial settings (stake_mode, stake, max_tp, max_sl,
                                  max_open_trades, max_symbols)
      stake   — update default stake live
      tp      — update session_tp live
      sl      — update session_sl live
    """
    if not CMD_FILE:
        log("No TBOT_CMD_FILE set — running without command file", "warn")
        return

    _last_mtime = 0.0

    while True:
        await asyncio.sleep(1)
        try:
            mtime = os.path.getmtime(CMD_FILE)
        except FileNotFoundError:
            continue
        except Exception:
            continue

        if mtime <= _last_mtime:
            continue
        _last_mtime = mtime

        try:
            with open(CMD_FILE, "r", encoding="utf-8") as f:
                cmd = json.load(f)
        except Exception as e:
            log_file("warn", f"cmd_watcher read error: {e}")
            continue

        action = cmd.get("action", "")
        log_file("info", f"cmd received: {cmd}")

        if action == "start":
            # Apply all startup settings from the web UI
            mode = cmd.get("stake_mode", "manual")
            S["stake_mode"] = mode

            mt = int(cmd.get("max_open_trades", 2))
            ms = int(cmd.get("max_symbols", 2))
            S["max_trades_per_sym"] = max(1, mt)
            S["max_syms_trading"]   = max(1, ms)

            if mode == "auto":
                # TP/SL/stake calculated from live balance (done in authorize handler)
                # If balance already known, apply immediately
                if S["balance"] > 0:
                    apply_auto_stake(S["balance"])
                log(f"Auto mode configured · MaxTrades/sym:{mt} · MaxSyms:{ms}", "ok")
            else:
                stake = float(cmd.get("stake", 1.0))
                tp    = float(cmd.get("max_tp", 0.0))
                sl    = float(cmd.get("max_sl", 0.0))
                S["default_stake"] = max(stake, 0.35)
                S["session_tp"]    = tp
                S["session_sl"]    = sl
                for sym in S["active_syms"]:
                    S["stakes"][sym] = S["default_stake"]
                log(f"Manual mode · Stake:${S['default_stake']:.2f} · TP:${tp:.2f} · SL:${sl:.2f} · MaxTrades/sym:{mt} · MaxSyms:{ms}", "ok")

            S["running"] = True
            log("Bot STARTED", "ok")

            # Clear the command file so it is not re-processed
            try:
                os.remove(CMD_FILE)
            except Exception:
                pass

        elif action == "stake":
            v = float(cmd.get("value", 0))
            if v > 0:
                S["default_stake"] = v
                for sym in S["active_syms"]:
                    S["stakes"][sym] = v
                log(f"Stake updated → ${v:.2f}", "ok")
            try: os.remove(CMD_FILE)
            except: pass

        elif action == "tp":
            v = float(cmd.get("value", 0))
            S["session_tp"] = v
            log(f"Take-profit updated → ${v:.2f}" if v > 0 else "Take-profit OFF", "ok")
            try: os.remove(CMD_FILE)
            except: pass

        elif action == "sl":
            v = float(cmd.get("value", 0))
            S["session_sl"] = v
            log(f"Stop-loss updated → ${v:.2f}" if v > 0 else "Stop-loss OFF", "ok")
            try: os.remove(CMD_FILE)
            except: pass

        elif action == "update_token":
            # Frontend sends this after user re-verifies credentials mid-session
            # The config file has already been updated by front.py — just reload it
            new_token, new_app_id = _read_config()
            if new_token:
                global WS_PATH
                WS_PATH    = f"/websockets/v3?app_id={new_app_id}"
                S["token"] = new_token
                log(f"Token updated from config (app_id={new_app_id}) — reconnecting", "ok")
                # Force WS reconnect so new token is used immediately
                ws = S.get("ws")
                if ws and not ws.get("closed"):
                    try:
                        ws["writer"].close()
                    except Exception:
                        pass
            else:
                log("update_token: no token found in config file", "warn")
            try: os.remove(CMD_FILE)
            except: pass

# ── WebSocket loop ─────────────────────────────────────────────────────────────
async def ws_loop():
    while True:
        try:
            # Re-read config on every (re)connect — picks up token/app_id updates
            # made via the frontend verify_and_save flow without restarting the bot
            fresh_token, fresh_app_id = _read_config()
            if fresh_token and fresh_token != S.get("token", ""):
                global WS_PATH
                WS_PATH    = f"/websockets/v3?app_id={fresh_app_id}"
                S["token"] = fresh_token
                log(f"Token refreshed from config (app_id={fresh_app_id})", "ok")

            log("Connecting...", "info")
            reader, writer = await ws_connect()
            S["ws"] = {"reader":reader,"writer":writer,"closed":False}
            S["connected"] = True
            log("Connected", "ok")
            await authorize()

            while True:
                op, payload = await ws_read_frame(reader)
                if op == 8:
                    break
                if op == 9:
                    writer.write(ws_frame(payload, op=10))
                    await writer.drain()
                    continue
                if op in (1,2):
                    try:
                        msg = json.loads(payload.decode())
                        await on_message(msg)
                    except Exception as e:
                        log(f"Msg err: {e}", "error")

        except Exception as e:
            import traceback
            log_file("error", f"ws_loop exception: {e}\n{traceback.format_exc()}")
            log(f"WS error: {e}", "error")
        finally:
            S["connected"] = False
            S["authorized"] = False
            if S["ws"]: S["ws"]["closed"] = True
            S["ws"] = None
        await asyncio.sleep(5)

# ── Config reader — always reads from TBOT_CONFIG file ───────────────────────
def _read_config():
    """
    Read the config_{safe}.json written by front.py start_session.
    Returns (api_token, app_id) tuple.
    Falls back gracefully if file is missing or malformed.
    """
    api_token = ""
    app_id    = "1089"   # Deriv public demo app_id default

    if not _cfg_file:
        return api_token, app_id

    if not os.path.exists(_cfg_file):
        log_file("warn", f"Config file not found: {_cfg_file}")
        return api_token, app_id

    try:
        with open(_cfg_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        api_token = str(cfg.get("api_token", "")).strip()
        app_id    = str(cfg.get("app_id",    app_id)).strip() or app_id
        log_file("info", f"Config loaded from {_cfg_file} — app_id={app_id} token={'set' if api_token else 'MISSING'}")
    except Exception as e:
        log_file("error", f"Config read error ({_cfg_file}): {e}")

    return api_token, app_id


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    # ── Step 1: read credentials from the config file front.py wrote ──────────
    api_token, app_id = _read_config()

    # ── If token is missing, poll the config file for up to 30 s ─────────────
    # This handles the "skip config" case where the file may not exist yet
    # or the user re-verifies after the bot has already started.
    if not api_token:
        sys.stdout.write(f"{YLW}⟳ Waiting for API token from config...{R}\r\n")
        sys.stdout.flush()
        for _ in range(30):
            await asyncio.sleep(1)
            api_token, app_id = _read_config()
            if api_token:
                break

    if not api_token:
        sys.stdout.write(f"{RED}✗ No API token found in config after 30s.{R}\r\n"
                         f"  Complete Step 2 (App Configuration) and restart.\r\n")
        sys.stdout.flush()
        return

    # ── Step 2: patch the WS URL with the user's app_id ──────────────────────
    global WS_PATH
    WS_PATH   = f"/websockets/v3?app_id={app_id}"
    S["token"] = api_token

    # Load all symbols — stakes will be set when "start" command arrives
    for sym in KNOWN_SYMS:
        S["active_syms"].append(sym)
        S["stakes"][sym] = 0.35   # placeholder — overwritten by start command

    # ── Step 3: load shared trade memory ──────────────────────────────────────
    _load_memory()
    wt = sum(len(v) for v in S["win_memory"].values())
    lt = sum(len(v) for v in S["loss_memory"].values())

    sys.stdout.write(f"{GRN}✓ TBOT Started{R}\r\n")
    sys.stdout.write(f"  {DIM}App ID  : {BLU}{app_id}{R}\r\n")
    sys.stdout.write(f"  {DIM}Token   : {GRN}{'*' * 4 + api_token[-4:]}{R}\r\n")
    sys.stdout.write(f"  {DIM}Memory  : {GRN}{wt} wins{DIM} / {RED}{lt} losses{DIM} (shared across all users){R}\r\n")
    sys.stdout.write(f"  {DIM}Symbols : {len(KNOWN_SYMS)} loaded{R}\r\n")
    sys.stdout.write(f"\r\n{YLW}Waiting for settings...{R}\r\n")
    sys.stdout.flush()

    # ── Step 4: run everything concurrently ───────────────────────────────────
    await asyncio.gather(
        ws_loop(),
        cmd_watcher(),
        dashboard_loop(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(f"\r\n{WHT}Bot stopped.{R}\r\n")
        sys.stdout.flush()
