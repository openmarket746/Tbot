import websocket
import json
import time
import threading
import os
import math
from collections import deque
import sys
try:
    from colorama import init, Fore, Back, Style
    # strip=False  → never remove ANSI codes (Render has no real TTY)
    # convert=False → don't convert to Win32 calls (Linux/Render only)
    # autoreset=True → reset colour after every print
    init(autoreset=True, strip=False, convert=False)
    HAS_COLOR = True
except ImportError:
    class _Noop:
        def __getattr__(self, _): return ""
    Fore = Back = Style = _Noop()
    HAS_COLOR = False

# ============================================================
#  RECONNECT SETTINGS
# ============================================================
RECONNECT_DELAY_MIN  = 3      # seconds before first retry
RECONNECT_DELAY_MAX  = 60     # cap on exponential backoff
RECONNECT_MAX_TRIES  = 0      # 0 = retry forever
PING_TIMEOUT_SECS    = 30     # watchdog triggers reconnect if no pong in this long

# Reconnect state (managed by run_with_reconnect)
_reconnect_delay     = RECONNECT_DELAY_MIN
_last_pong_ts        = 0.0    # updated whenever any message arrives (server is alive)
_reconnecting        = False
_reconnect_lock      = threading.Lock()

# ============================================================
#  FILE PATHS
# ============================================================
# All data files are namespaced per-user so multiple concurrent users
# never share state files.
#
# TBOT_USER  (set by front.py) = sanitised license name — STABLE across reconnects.
#            Persistent files (win/loss memory, summary, lock) use this key.
#
# TBOT_SID   (set by front.py) = socket connection ID — changes each session.
#            Temp files (session config, open_trades) use this key.

# TBOT_DATA_DIR env var redirects all user data files to a persistent disk
# (e.g. Render Disk at /data, or any mounted volume). Falls back to script dir.
_BASE_DIR = os.environ.get("TBOT_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
_SID      = os.environ.get("TBOT_SID",  "default")
_USER     = os.environ.get("TBOT_USER", _SID)   # stable license name

def _sid_path(name):
    """Session-scoped path — keyed by SID, discarded on disconnect."""
    base, ext = os.path.splitext(name)
    return os.path.join(_BASE_DIR, f"{base}_{_SID}{ext}")

def _user_path(name):
    """Persistent path — keyed by license name, survives reconnects."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in _USER)
    base, ext = os.path.splitext(name)
    return os.path.join(_BASE_DIR, f"{base}_{safe}{ext}")

# ── SHARED global memory — all users train the same brain ───────────────
# win.json and loss.json are shared across ALL users.
# Upload your pre-trained files to the repo root and they become the
# starting point. Every trade any user takes appends to these files.
WIN_MEMORY       = os.path.join(_BASE_DIR, "win.json")
LOSS_MEMORY      = os.path.join(_BASE_DIR, "loss.json")

# ── Per-user files — private to each license ─────────────────────────────
SUMMARY_FILE     = _user_path("trade_summary.json")
LOCK_FILE        = _user_path("bot_lock.json")

# Session-scoped (keyed by SID)
CONFIG_FILE      = os.environ.get("TBOT_CONFIG", _sid_path("config.json"))
OPEN_TRADES_FILE = _sid_path("open_trades.json")   # persisted across reconnects within same session
CMD_FILE         = os.environ.get("TBOT_CMD_FILE", _sid_path("cmd.json"))  # live command queue
LOCK_HOURS       = 24

# ============================================================
#  DAILY TP / SL  (simple — no phases)
# ============================================================
# The bot trades continuously until the daily TP or SL is hit,
# then locks for 24 h before restarting.

# ============================================================
#  SYMBOL POOL  — bot selects top 5 from this list automatically
#  No config file needed; symbols are never read from config.
# ============================================================
SYMBOL_POOL = [
    "stpRNG2","stpRNG3","stpRNG4","stpRNG5",
    "1HZ10V","R_10","1HZ15V","1HZ25V",
    "R_25","1HZ30V","1HZ50V","R_50","1HZ75V","R_75","1HZ90V","1HZ100V","R_100",
    "JD10","JD25","JD50","JD75","JD100",
]

SYMBOL_RESELECT_INTERVAL = 120   # seconds between auto-reselections

# Reselection state — updated by the symbol-picker thread
_last_reselect_ts   = 0.0
_reselect_lock      = threading.Lock()
_next_reselect_ts   = 0.0   # epoch of next scheduled reselection

# ============================================================
#  DEFAULT CONFIG
# ============================================================
DEFAULT_CONFIG = {
    "app_id": "111639",
    "api_token": "G5mKh1DgynEGGsZ",
    "pairs": []   # deliberately empty — symbol selection is self-contained
}

# ============================================================
#  TRADING CONSTANTS
# ============================================================
DURATION        = 2
CURRENCY        = "USD"
MAX_OPEN_TRADES = 2
TOP_N           = 5

BOT_SIGNAL_STABILITY = 3

SIGNAL_THRESHOLD       = 55
CURR_BUY_THRESHOLD     = 52
CURR_SELL_THRESHOLD    = 45
MIN_WIN_RATE           = 0.40
MIN_TRADES_FOR_WINRATE = 5
LOSS_COOLDOWN_TICKS    = 3

# ---- PnL sleep trigger ----
# When session net PnL moves by this ratio of daily SL/TP, pause new trades for 1 min
# E.g. 0.5 → sleep when profit/loss reaches 50% of the daily target
PNL_SLEEP_RATIO        = 0.50   # fraction of MAX_TP or MAX_SL that triggers a 1-min pause
PNL_SLEEP_SECONDS      = 60     # how long to pause new trades (keep connection alive)

# ---- Memory similarity thresholds ----
# A new setup scoring >= this vs a LOSS record is BLOCKED
MEMORY_BLOCK_THRESHOLD = 0.82
# A new setup scoring >= this vs a WIN record gets a BOOST confirmation
MEMORY_BOOST_THRESHOLD = 0.80
# Max entries scanned per check (keeps it O(N) bounded, no lag)
MEMORY_SCAN_LIMIT      = 200

# ============================================================
#  RUNTIME GLOBALS
# ============================================================
STAKE           = 0.0
MAX_TP          = 0.0
MAX_SL          = 0.0
STAKE_MODE      = ""
ACCOUNT_BALANCE = 0.0

ACTIVE_SYMBOL      = None
ACTIVE_SYMBOLS     = set()   # set of symbols currently holding open/in-flight trades
active_symbol_lock = threading.Lock()
MAX_SYMBOLS        = 1       # configured at startup in both auto and manual mode

config_lock      = threading.Lock()
CONFIG           = {}
CONFIG_PAIRS_MAP = {}

# ---- Trade sleep state ----
_trading_paused_until = 0.0   # epoch timestamp; 0 = not paused

# In-memory mirrors of win.json / loss.json — loaded once at startup
_win_memory  = []
_loss_memory = []
_memory_lock = threading.Lock()


# ============================================================
#  TRADE MEMORY  —  fingerprinting & similarity engine
# ============================================================

def _candle_signature(candle):
    """
    Convert one candle into a human-readable chart-like descriptor.
    Returns a compact string so the saved JSON reads like a chart, e.g.:
      "Bullish(body:78% upper:14% lower:8%)"  or  "Bearish(body:55% upper:30% lower:15%)"
    Also returns the underlying numeric dict for the similarity engine.
    """
    op = float(candle.get("open",  0) or 0)
    cl = float(candle.get("close", 0) or 0)
    hi = float(candle.get("high",  max(op, cl)) or max(op, cl))
    lo = float(candle.get("low",   min(op, cl)) or min(op, cl))
    body = cl - op
    rng  = hi - lo if hi != lo else 1e-9

    dir_val   = 1 if body > 0 else (-1 if body < 0 else 0)
    body_pct  = round(abs(body) / rng, 3)
    upper_pct = round((hi - max(op, cl)) / rng, 3)
    lower_pct = round((min(op, cl) - lo) / rng, 3)

    if dir_val == 1:
        label = "Bullish"
    elif dir_val == -1:
        label = "Bearish"
    else:
        label = "Doji"

    # Wick descriptions
    def _wick(pct):
        if pct < 0.05:  return "no"
        if pct < 0.20:  return "small"
        if pct < 0.40:  return "med"
        return "long"

    readable = (
        f"{label}(body:{int(body_pct*100)}%"
        f" up:{_wick(upper_pct)}-{int(upper_pct*100)}%"
        f" dn:{_wick(lower_pct)}-{int(lower_pct*100)}%)"
    )

    # Keep numerics for similarity engine
    return {
        "label": readable,          # human-readable — saved to JSON
        "dir":   dir_val,           # numeric — used by similarity engine only
        "body":  body_pct,
        "upper": upper_pct,
        "lower": lower_pct,
    }


def build_setup_fingerprint(symbol, direction, candles, ind, htf_ind,
                             ten_buy, ten_sell, curr_buy, displacement, atr_val):
    """
    Build a symbol-agnostic, price-scale-agnostic setup fingerprint.
    All values are normalised so the same pattern on stpRNG matches stpRNG2, etc.
    """
    last10      = candles[-10:] if len(candles) >= 10 else candles
    candle_sigs = [_candle_signature(c) for c in last10]
    # Human-readable chart line: oldest left → newest right (like reading a chart)
    chart_line  = "  ".join(sig["label"] for sig in candle_sigs)

    # Displacement normalised by ATR  → scale-free
    norm_disp = round(displacement / atr_val, 3) if (atr_val and atr_val > 0) else 0.0

    # RSI bucket
    htf_rsi = (htf_ind or {}).get("htf_rsi")
    if   htf_rsi is None:  rsi_bucket = None
    elif htf_rsi < 30:     rsi_bucket = "OS"
    elif htf_rsi < 50:     rsi_bucket = "LOW"
    elif htf_rsi < 70:     rsi_bucket = "MID"
    else:                  rsi_bucket = "OB"

    # Market condition snapshot
    market_condition = {
        "trend":         (ind     or {}).get("trend"),
        "htf_trend":     (htf_ind or {}).get("htf_trend"),
        "exhausted":     (ind     or {}).get("exhausted", False),
        "htf_exhausted": (htf_ind or {}).get("htf_exhausted", False),
        "rsi_bucket":    rsi_bucket,
        "ema_fast_gt_slow": (
            (ind or {}).get("ema_fast", 0) > (ind or {}).get("ema_slow", 0)
            if (ind or {}).get("ema_fast") and (ind or {}).get("ema_slow")
            else None
        ),
    }

    return {
        # ---- identity / audit ----
        "timestamp":        int(time.time()),
        "symbol":           symbol,
        "direction":        direction,        # "CALL" / "PUT"
        # ---- market condition ----
        "market_condition": market_condition,
        # ---- tick dominance (normalised 0-1) ----
        "ten_buy":          round((ten_buy  or 0) / 100, 4),
        "ten_sell":         round((ten_sell or 0) / 100, 4),
        "curr_buy":         round((curr_buy or 0) / 100, 4),
        # ---- displacement ----
        "norm_disp":        norm_disp,
        # ---- last-10 candle pattern (human-readable, oldest→newest like a chart) ----
        "chart_pattern":    chart_line,
        # ---- numeric candle data for similarity engine ----
        "candles":          [{"dir": s["dir"], "body": s["body"],
                              "upper": s["upper"], "lower": s["lower"]}
                             for s in candle_sigs],
        # ---- indicator snapshot (for human review) ----
        "ind_snapshot": {
            "ema_fast":  round((ind or {}).get("ema_fast",  0) or 0, 6),
            "ema_slow":  round((ind or {}).get("ema_slow",  0) or 0, 6),
            "atr_val":   round(atr_val or 0, 6),
            "htf_rsi":   round(htf_rsi or 0, 2) if htf_rsi else None,
        },
    }


def _cosine_candles(a_list, b_list):
    """Fast cosine similarity over two lists of candle-signature dicts."""
    keys = ("dir", "body", "upper", "lower")
    n    = min(len(a_list), len(b_list))
    if n == 0:
        return 0.0
    dot = ss_a = ss_b = 0.0
    for i in range(n):
        for k in keys:
            va = a_list[i].get(k, 0)
            vb = b_list[i].get(k, 0)
            dot  += va * vb
            ss_a += va * va
            ss_b += vb * vb
    denom = math.sqrt(ss_a) * math.sqrt(ss_b)
    return dot / denom if denom > 0 else 0.0


def setup_similarity(fp_a, fp_b):
    """
    Composite similarity [0, 1] between two fingerprints.
    Completely symbol-agnostic — reusable across all pairs.

    Weights
    -------
    Candle pattern  : 40 %
    Direction match : 20 %
    Trend alignment : 15 %
    Tick dominance  : 15 %
    RSI bucket      : 10 %
    """
    # Direction
    dir_score = 1.0 if fp_a.get("direction") == fp_b.get("direction") else 0.0

    # Trend (1-min + HTF both match)
    mc_a = fp_a.get("market_condition") or {}
    mc_b = fp_b.get("market_condition") or {}
    trend_score = 1.0 if (
        mc_a.get("trend")     == mc_b.get("trend") and
        mc_a.get("htf_trend") == mc_b.get("htf_trend")
    ) else 0.0

    # Tick dominance (already normalised 0-1)
    tick_score = (
        (1.0 - abs((fp_a.get("ten_buy",  0) or 0) - (fp_b.get("ten_buy",  0) or 0))) +
        (1.0 - abs((fp_a.get("curr_buy", 0) or 0) - (fp_b.get("curr_buy", 0) or 0)))
    ) / 2.0

    # RSI bucket
    rsi_score = 1.0 if mc_a.get("rsi_bucket") == mc_b.get("rsi_bucket") else 0.0

    # Candle shapes
    candle_score = _cosine_candles(fp_a.get("candles", []), fp_b.get("candles", []))

    score = (
        0.40 * candle_score +
        0.20 * dir_score    +
        0.15 * trend_score  +
        0.15 * tick_score   +
        0.10 * rsi_score
    )
    return round(score, 4)


def _load_memory_file(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _append_memory_file(path, record):
    """Append one record to a JSON-array file (atomic write)."""
    records = _load_memory_file(path)
    records.append(record)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(records, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(Fore.YELLOW + f"[Memory] Could not write {path}: {e}")


def _save_memory_snapshot():
    """Write a combined snapshot of shared memory for cross-deploy persistence.
    Uses a single shared snapshot file since win/loss memory is global."""
    snap = os.path.join(_BASE_DIR, "memory_snapshot.json")
    try:
        with _memory_lock:
            data = {"wins": list(_win_memory), "losses": list(_loss_memory),
                    "saved_at": time.time(), "contributors": "shared"}
        tmp = snap + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, snap)
    except Exception as e:
        print(f"[Memory] Snapshot save failed: {e}", flush=True)


_file_write_lock = threading.Lock()  # global lock for shared file writes

def save_trade_memory(fingerprint, won):
    """Persist fingerprint to shared win.json / loss.json.
    Thread-safe — multiple users can trade simultaneously.
    All users contribute to and benefit from the shared memory."""
    path = WIN_MEMORY if won else LOSS_MEMORY
    with _file_write_lock:  # prevent concurrent writes corrupting the file
        _append_memory_file(path, fingerprint)
    with _memory_lock:
        target = _win_memory if won else _loss_memory
        target.append(fingerprint)
        if len(target) > MEMORY_SCAN_LIMIT:
            del target[0]
    # Save snapshot after every trade so it survives redeployments
    _save_memory_snapshot()


def load_memory_on_startup():
    """Load existing win/loss memories into RAM — called once before the WebSocket starts."""
    global _win_memory, _loss_memory
    wins   = _load_memory_file(WIN_MEMORY)
    losses = _load_memory_file(LOSS_MEMORY)
    with _memory_lock:
        _win_memory  = wins[-MEMORY_SCAN_LIMIT:]
        _loss_memory = losses[-MEMORY_SCAN_LIMIT:]
    wc = len(_win_memory)
    lc = len(_loss_memory)
    print(Fore.CYAN + Style.BRIGHT +
          f"[Memory] Loaded {wc} wins & {lc} losses from SHARED memory (all users).")


def check_setup_against_memory(fingerprint):
    """
    Compare a candidate setup against all remembered wins & losses.

    Returns  (action, best_score, match_type)
      "block"   → strongly resembles a known loss  → skip trade
      "boost"   → strongly resembles a known win   → trade with confidence
      "neutral" → no strong historical match        → trade normally

    Design goals
    ------------
    • Pure in-memory scan — zero file I/O, no sleep, no blocking.
    • Thread-safe via a shallow list copy before iteration.
    • O(N) where N ≤ MEMORY_SCAN_LIMIT (200) — negligible on every tick.
    """
    best_loss = 0.0
    best_win  = 0.0

    with _memory_lock:
        losses_snap = list(_loss_memory[-MEMORY_SCAN_LIMIT:])
        wins_snap   = list(_win_memory[-MEMORY_SCAN_LIMIT:])

    for fp in losses_snap:
        s = setup_similarity(fingerprint, fp)
        if s > best_loss:
            best_loss = s

    for fp in wins_snap:
        s = setup_similarity(fingerprint, fp)
        if s > best_win:
            best_win = s

    # Block only when loss-similarity clearly dominates win-similarity
    if best_loss >= MEMORY_BLOCK_THRESHOLD and best_loss > best_win + 0.05:
        return "block", best_loss, "loss"

    if best_win >= MEMORY_BOOST_THRESHOLD:
        return "boost", best_win, "win"

    return "neutral", max(best_loss, best_win), "none"


# ============================================================
#  CONFIG LOAD
# ============================================================
def load_or_create_config():
    global CONFIG, CONFIG_PAIRS_MAP
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
        except Exception:
            os.rename(CONFIG_FILE, CONFIG_FILE + ".bak." + str(int(time.time())))
            cfg = DEFAULT_CONFIG.copy()
            with open(CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=4)
            print(Fore.YELLOW + f"Created default {CONFIG_FILE} (recovered from corrupt).")
    else:
        cfg = DEFAULT_CONFIG.copy()
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
        print(Fore.YELLOW + f"Created default {CONFIG_FILE}. Update it with your app_id, api_token and pairs.")

    cfg.setdefault("app_id",    DEFAULT_CONFIG["app_id"])
    cfg.setdefault("api_token", DEFAULT_CONFIG["api_token"])
    cfg.setdefault("pairs",     DEFAULT_CONFIG["pairs"])

    pairs_map = {}
    for p in cfg["pairs"]:
        if isinstance(p, str):
            pairs_map[p] = {"trend": None, "signal": None, "strength": 0}
        elif isinstance(p, dict):
            sym = p.get("symbol")
            if sym:
                pairs_map[sym] = {
                    "trend":    p.get("trend"),
                    "signal":   p.get("signal"),
                    "strength": p.get("strength", 0),
                }
    with config_lock:
        CONFIG = cfg
        CONFIG_PAIRS_MAP.clear()
        CONFIG_PAIRS_MAP.update(pairs_map)
    return cfg


config    = load_or_create_config()
APP_ID    = config.get("app_id")
API_TOKEN = config.get("api_token")
ws_url    = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"


# ============================================================
#  PnL HISTORY  (for terminal graph)
# ============================================================
PNL_HISTORY = []          # list of cumulative PnL floats after each closed trade
_pnl_lock   = threading.Lock()

def record_pnl_point(cumulative_pnl):
    """Append a cumulative PnL snapshot (called after each trade close)."""
    with _pnl_lock:
        PNL_HISTORY.append(round(cumulative_pnl, 4))
        # Keep only the last 60 points so the graph doesn't grow forever
        if len(PNL_HISTORY) > 60:
            del PNL_HISTORY[0]


def render_pnl_graph(width=70, height=12):
    """
    Render a clear ASCII PnL chart with:
    - Labelled Y-axis (profit/loss scale)
    - Zero baseline clearly marked
    - Green bars above zero, red bars below
    - Peak / valley / current markers
    - Sleep indicator if trading is paused
    Returns a list of coloured strings (one per row).
    """
    with _pnl_lock:
        data = list(PNL_HISTORY)

    now_ts = time.time()
    sleep_active = _trading_paused_until > now_ts
    sleep_rem    = max(0, int(_trading_paused_until - now_ts))

    if not data:
        lines = [Fore.WHITE + "  No trades recorded yet — graph will appear after first close."]
        if sleep_active:
            lines.append(Fore.YELLOW + Style.BRIGHT +
                         f"  💤 TRADING PAUSED  {sleep_rem}s remaining")
        return lines

    pts = data[-width:]
    n   = len(pts)

    # Y range — always include 0
    raw_max = max(pts + [0])
    raw_min = min(pts + [0])
    y_span  = raw_max - raw_min
    # Add 10% padding top and bottom so bars don't touch the edge
    pad     = y_span * 0.10 if y_span > 0 else 0.10
    y_top   = raw_max + pad
    y_bot   = raw_min - pad
    y_range = y_top - y_bot if y_top != y_bot else 1.0

    def val_to_row(v):
        """Convert a PnL value to a grid row index (0 = top, height-1 = bottom)."""
        return int((y_top - v) / y_range * (height - 1))

    zero_row = val_to_row(0)

    # Build the grid: list of lists of characters
    grid = [[" " for _ in range(n)] for _ in range(height)]

    for col, val in enumerate(pts):
        bar_row  = val_to_row(val)
        is_green = val >= 0

        # Fill bar from zero_row to bar_row (or vice versa)
        r_start = min(zero_row, bar_row)
        r_end   = max(zero_row, bar_row)
        for r in range(r_start, r_end + 1):
            grid[r][col] = "G" if is_green else "R"   # colour tag, rendered below

    # Mark zero row with dashes where no bar covers it
    for col in range(n):
        if grid[zero_row][col] == " ":
            grid[zero_row][col] = "0"

    # Mark peak and valley columns
    peak_col   = pts.index(max(pts))
    valley_col = pts.index(min(pts))
    if 0 <= peak_col < n:
        grid[val_to_row(max(pts))][peak_col] = "P"
    if 0 <= valley_col < n:
        grid[val_to_row(min(pts))][valley_col] = "V"

    # Mark last value
    last_row = val_to_row(pts[-1])
    if 0 <= last_row < height:
        grid[last_row][n - 1] = "L"

    # Y-axis label width
    label_w = 8

    lines = []
    for r in range(height):
        # Y label at regular intervals + zero row + top/bottom
        row_val = y_top - (r / (height - 1)) * y_range
        if r == 0 or r == height - 1 or r == zero_row or r % max(1, height // 4) == 0:
            lbl = f"{row_val:+.2f}"
        else:
            lbl = ""

        # Zero row separator
        if r == zero_row:
            axis_char = Fore.WHITE + "─"
        else:
            axis_char = Fore.CYAN  + "│"

        row_str = Fore.CYAN + f"{lbl:>{label_w}} " + axis_char + " "

        for col in range(n):
            ch = grid[r][col]
            if ch == "G":
                row_str += Fore.GREEN + "█"
            elif ch == "R":
                row_str += Fore.RED + "█"
            elif ch == "0":
                row_str += Fore.WHITE + Style.DIM + "─"
            elif ch == "P":
                row_str += Fore.GREEN + Style.BRIGHT + "▲"
            elif ch == "V":
                row_str += Fore.RED   + Style.BRIGHT + "▼"
            elif ch == "L":
                color = Fore.GREEN if pts[-1] >= 0 else Fore.RED
                row_str += color + Style.BRIGHT + "◆"
            else:
                row_str += " "
        lines.append(row_str)

    # X-axis bottom
    lines.append(Fore.CYAN + " " * (label_w + 3) + "└" + "─" * n)

    # Stats row
    cur_pnl   = pts[-1]
    pnl_color = Fore.GREEN if cur_pnl >= 0 else Fore.RED
    lines.append(
        Fore.WHITE + f"  {'Trades:':8}{len(data):<5}"
        + Fore.GREEN  + f"  ▲ Peak: {max(pts):>+7.2f}"
        + Fore.RED    + f"  ▼ Valley: {min(pts):>+7.2f}"
        + pnl_color   + f"  ◆ Now: {cur_pnl:>+7.2f}"
    )

    # Legend
    lines.append(
        Fore.GREEN  + "  █ Profit  "
        + Fore.RED  + "█ Loss  "
        + Fore.WHITE + Style.DIM + "─ Zero  "
        + Fore.GREEN + Style.BRIGHT + "▲ Peak  "
        + Fore.RED   + Style.BRIGHT + "▼ Valley  "
        + (Fore.GREEN if cur_pnl >= 0 else Fore.RED) + Style.BRIGHT + "◆ Current"
    )

    if sleep_active:
        lines.append(Fore.YELLOW + Style.BRIGHT +
                     f"  💤 TRADING PAUSED  —  new trades blocked for {sleep_rem}s "
                     f"(PnL threshold reached)")

    return lines


# ============================================================
#  LOCK FILE
# ============================================================
def write_lock(reason):
    expires_at = time.time() + LOCK_HOURS * 3600
    with open(LOCK_FILE, "w") as f:
        json.dump({"expires_at": expires_at, "reason": reason}, f, indent=4)


def check_lock():
    if not os.path.exists(LOCK_FILE):
        return
    try:
        with open(LOCK_FILE, "r") as f:
            lock = json.load(f)
        expires_at = lock.get("expires_at", 0)
        reason     = lock.get("reason", "TP/SL limit hit")
        remaining  = expires_at - time.time()
        if remaining > 0:
            hrs  = int(remaining // 3600)
            mins = int((remaining % 3600) // 60)
            secs = int(remaining % 60)
            print(Fore.RED + Style.BRIGHT + f"\n🔒 Bot is locked — {reason}")
            print(Fore.RED + f"   Time remaining : {hrs}h {mins}m {secs}s")
            print(Fore.RED + f"   Lock expires at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires_at))}")
            os._exit(0)
        else:
            os.remove(LOCK_FILE)
            print(Fore.GREEN + "Lock expired. Starting bot...")
    except Exception as e:
        print(Fore.YELLOW + f"Warning: could not read lock file ({e}). Continuing...")


# ============================================================
#  STARTUP SEQUENCE
# ============================================================


# ============================================================
#  COMMAND FILE — replaces stdin input() for cloud deployments
#  front.py writes commands here; this bot reads and applies them.
# ============================================================

def _read_cmd_file():
    """Read and clear the command file. Returns dict or None."""
    if not os.path.exists(CMD_FILE):
        return None
    try:
        with open(CMD_FILE, "r") as f:
            data = json.load(f)
        os.remove(CMD_FILE)   # consume it
        return data
    except Exception:
        return None

def _wait_for_cmd_file(timeout=300):
    """
    Block until CMD_FILE appears (user submitted settings from browser).
    Prints a waiting message so the terminal shows something.
    Returns the command dict, or None on timeout.
    """
    print(Fore.CYAN + Style.BRIGHT +
          "\n  Waiting for settings from the browser...\n"
          "  Set your stake mode, TP and SL in the control panel, then click Start.\n",
          flush=True)
    elapsed = 0
    while elapsed < timeout:
        cmd = _read_cmd_file()
        if cmd:
            return cmd
        time.sleep(1)
        elapsed += 1
        if elapsed % 30 == 0:
            print(Fore.YELLOW + f"  Still waiting... ({elapsed}s)", flush=True)
    return None

def _watch_cmd_file():
    """
    Background thread — watches CMD_FILE for live commands while bot is running.
    Supported commands:
      stake:<float>  — update STAKE
      tp:<float>     — update MAX_TP
      sl:<float>     — update MAX_SL
      stop           — graceful shutdown
    """
    global STAKE, MAX_TP, MAX_SL
    while True:
        time.sleep(1)
        cmd = _read_cmd_file()
        if not cmd:
            continue
        action = cmd.get("action", "")
        value  = cmd.get("value")
        try:
            if action == "stake" and value is not None:
                STAKE = float(value)
                print(Fore.GREEN + Style.BRIGHT + f"\n[CMD] Stake updated → {STAKE}", flush=True)
            elif action == "tp" and value is not None:
                MAX_TP = float(value)
                print(Fore.GREEN + Style.BRIGHT + f"\n[CMD] Take Profit updated → {MAX_TP}", flush=True)
            elif action == "sl" and value is not None:
                MAX_SL = float(value)
                print(Fore.GREEN + Style.BRIGHT + f"\n[CMD] Stop Loss updated → {MAX_SL}", flush=True)
            elif action == "stop":
                print(Fore.RED + Style.BRIGHT + "\n[CMD] Stop received — shutting down.", flush=True)
                os._exit(0)
        except Exception as e:
            print(Fore.YELLOW + f"[CMD] Error applying command: {e}", flush=True)

check_lock()

# ── Restore win/loss memory from snapshot if fresh files are empty ────────────
def _restore_memory_snapshot():
    """
    On cloud deployments (Render free tier) win.json and loss.json are wiped
    on each redeploy. This restores them from the shared snapshot file.
    All users benefit from the combined training data.
    """
    snap = os.path.join(_BASE_DIR, "memory_snapshot.json")
    w_empty = not os.path.exists(WIN_MEMORY)  or os.path.getsize(WIN_MEMORY)  < 5
    l_empty = not os.path.exists(LOSS_MEMORY) or os.path.getsize(LOSS_MEMORY) < 5

    if (w_empty or l_empty) and os.path.exists(snap):
        try:
            with open(snap) as f:
                data = json.load(f)
            if w_empty and data.get("wins"):
                with open(WIN_MEMORY, "w") as f:
                    json.dump(data["wins"], f, indent=2)
                print(f"[Memory] Restored {len(data['wins'])} wins from snapshot", flush=True)
            if l_empty and data.get("losses"):
                with open(LOSS_MEMORY, "w") as f:
                    json.dump(data["losses"], f, indent=2)
                print(f"[Memory] Restored {len(data['losses'])} losses from snapshot", flush=True)
        except Exception as e:
            print(f"[Memory] Snapshot restore failed: {e}", flush=True)

_restore_memory_snapshot()
load_memory_on_startup()

print(Style.BRIGHT + Fore.CYAN + "=" * 60)
print(Style.BRIGHT + Fore.CYAN + "         DERIV AUTO-TRADING BOT  —  STARTUP")
print(Style.BRIGHT + Fore.CYAN + "=" * 60)

# ── Read settings from command file (sent by browser) ─────────────────────────
# This replaces all the input() calls so the bot works on cloud deployments
# where there is no interactive terminal connected.
_startup_cmd = _wait_for_cmd_file(timeout=300)

if _startup_cmd is None:
    print(Fore.RED + "Timed out waiting for settings. Exiting.", flush=True)
    raise SystemExit(1)

STAKE_MODE = _startup_cmd.get("stake_mode", "manual")
MAX_OPEN_TRADES = int(_startup_cmd.get("max_open_trades", MAX_OPEN_TRADES))
MAX_SYMBOLS     = int(_startup_cmd.get("max_symbols", MAX_SYMBOLS))

if STAKE_MODE == "manual":
    STAKE  = float(_startup_cmd.get("stake",  0))
    MAX_TP = float(_startup_cmd.get("max_tp", 0))
    MAX_SL = float(_startup_cmd.get("max_sl", 0))
    if MAX_OPEN_TRADES < 1: MAX_OPEN_TRADES = 1
    if MAX_SYMBOLS     < 1: MAX_SYMBOLS     = 1
    print(Fore.GREEN + Style.BRIGHT +
          f"\n✔ Manual — Stake: {STAKE}  |  TP: {MAX_TP}  |  SL: {MAX_SL}  |  "
          f"Trades/sym: {MAX_OPEN_TRADES}  |  Symbols: {MAX_SYMBOLS}", flush=True)
else:
    STAKE_MODE = "auto"
    if MAX_OPEN_TRADES < 1: MAX_OPEN_TRADES = 1
    if MAX_SYMBOLS     < 1: MAX_SYMBOLS     = 1
    print(Fore.GREEN + Style.BRIGHT +
          f"\n✔ Auto Stake — Trades/sym: {MAX_OPEN_TRADES}  |  Symbols: {MAX_SYMBOLS}  "
          f"(TP/SL/Stake set from balance)", flush=True)

# Start the live command watcher in background
threading.Thread(target=_watch_cmd_file, daemon=True, name="cmd-watcher").start()

# Start heartbeat writer — writes a timestamp file every 30s
# /keepalive on the web server reads these to confirm bots are alive
def _heartbeat_writer():
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in _USER)
    hb   = os.path.join(_BASE_DIR, f"heartbeat_{safe}.txt")
    while True:
        try:
            with open(hb, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        time.sleep(30)

threading.Thread(target=_heartbeat_writer, daemon=True, name="heartbeat").start()


# ============================================================
#  GLOBAL STATE
# ============================================================
symbols        = []
symbol_state   = {}
top_symbols    = []
symbols_loaded = False
history_count  = 0

active_trades        = {}
processed_contracts  = set()
proposals            = {}
pending_fingerprints = {}      # contract_id -> fingerprint captured at entry
proposal_fps         = {}      # proposal_id  -> fingerprint (bridges proposal->buy gap)


summary = {
    "total_trades": 0,
    "wins":         0,
    "losses":       0,
    "net_profit":   0,
    "trades":       []
}


# ============================================================
#  AUTO-STAKE
# ============================================================
def apply_auto_stake(balance):
    global STAKE, MAX_TP, MAX_SL, ACCOUNT_BALANCE
    ACCOUNT_BALANCE = balance
    MAX_TP = round(balance * 0.10, 2)
    MAX_SL = round(balance * 0.05, 2)
    if STAKE_MODE == "auto" and STAKE == 0.0:
        # Stake = SL / 4  →  4 consecutive losses exhaust the daily SL
        STAKE = round(MAX_SL / 4, 2)
    print(Fore.GREEN + Style.BRIGHT +
          f"\n✔ Auto Stake  |  Balance: {balance}  |  "
          f"Daily TP: {MAX_TP}  |  Daily SL: {MAX_SL}  |  Stake/trade: {STAKE}")


# ============================================================
#  SUMMARY
# ============================================================
def save_summary():
    tmp = SUMMARY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(summary, f, indent=4)
    os.replace(tmp, SUMMARY_FILE)


# ============================================================
#  OPEN TRADES PERSISTENCE
# ============================================================
def save_open_trades():
    """
    Persist all currently tracked trades (open + recently closed) to disk.
    Called after every open, update, or close so reconnects can recover state.
    Only open trades are actually needed on reconnect, but we store all to
    avoid re-counting a trade that closed while disconnected.
    """
    # Strip non-serialisable 'contract' blobs — we only need the lightweight fields
    serialisable = {}
    for cid, t in active_trades.items():
        serialisable[str(cid)] = {
            "symbol":    t.get("symbol",    "-"),
            "direction": t.get("direction"),
            "stake":     t.get("stake",     0),
            "bought_at": t.get("bought_at", 0),
            "status":    t.get("status",    "open"),
            "pnl":       t.get("pnl"),
            "profit":    t.get("profit"),
        }
    tmp = OPEN_TRADES_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(serialisable, f, indent=2)
        os.replace(tmp, OPEN_TRADES_FILE)
    except Exception as e:
        print(Fore.YELLOW + f"[Trades] Could not save open_trades.json: {e}")


def load_open_trades():
    """
    Load persisted trades from disk into active_trades.
    Returns the list of contract_ids that are still open (need re-subscription).
    """
    if not os.path.exists(OPEN_TRADES_FILE):
        return []
    try:
        with open(OPEN_TRADES_FILE, "r") as f:
            saved = json.load(f)
    except Exception as e:
        print(Fore.YELLOW + f"[Trades] Could not read open_trades.json: {e}")
        return []

    restored = 0
    open_ids  = []
    for cid_str, t in saved.items():
        try:
            cid = int(cid_str)
        except ValueError:
            cid = cid_str
        active_trades[cid] = t
        if t.get("status") == "open":
            open_ids.append(cid)
            restored += 1

    if restored:
        print(Fore.CYAN + Style.BRIGHT +
              f"[Trades] Restored {restored} open trade(s) from disk: {open_ids}")
    return open_ids


# ============================================================
#  SHUTDOWN
# ============================================================
def shutdown(ws, reason="TP/SL limit hit"):
    print(Fore.RED + Style.BRIGHT + f"\n{reason}. Closing bot and applying {LOCK_HOURS}h lock...")
    save_summary()
    write_lock(reason)
    try:
        ws.close()
    except Exception:
        pass
    os._exit(0)   # hard exit — bypasses the reconnect loop intentionally


# ============================================================
#  KEEP ALIVE
# ============================================================
def start_ping(ws):
    def run():
        while True:
            try:
                ws.send(json.dumps({"ping": 1}))
            except Exception:
                break
            time.sleep(2)
    threading.Thread(target=run, daemon=True).start()


# ============================================================
#  CONTRACT MONITOR
# ============================================================
def contract_monitor_loop(ws):
    while True:
        try:
            # Only poll contracts still explicitly marked as open
            open_ids = [
                cid for cid, t in list(active_trades.items())
                if t.get("status") == "open"
            ]
            for contract_id in open_ids:
                try:
                    ws.send(json.dumps({
                        "proposal_open_contract": 1,
                        "contract_id": contract_id
                    }))
                except Exception:
                    pass
        except Exception:
            break
        time.sleep(2)


# ============================================================
#  CONFIG WATCHER
# ============================================================
def watch_config_loop(ws, interval=300):
    last_mtime = None
    while True:
        try:
            mtime = os.path.getmtime(CONFIG_FILE)
        except Exception:
            mtime = None
        if mtime and mtime != last_mtime:
            last_mtime = mtime
            try:
                with open(CONFIG_FILE, "r") as f:
                    cfg = json.load(f)
                new_map = {}
                for p in cfg.get("pairs", []):
                    if isinstance(p, str):
                        new_map[p] = {"trend": None, "signal": None, "strength": 0}
                    elif isinstance(p, dict):
                        sym = p.get("symbol")
                        if sym:
                            new_map[sym] = {
                                "trend":    p.get("trend"),
                                "signal":   p.get("signal"),
                                "strength": p.get("strength", 0),
                            }
                with config_lock:
                    old_keys = set(CONFIG_PAIRS_MAP.keys())
                    added    = set(new_map.keys()) - old_keys
                    CONFIG_PAIRS_MAP.clear()
                    CONFIG_PAIRS_MAP.update(new_map)
                for symbol in added:
                    if symbol not in symbol_state:
                        symbol_state[symbol] = _blank_state()
                        try:
                            ws.send(json.dumps({"ticks_history": symbol, "granularity": 60,  "count": 60, "end": "latest", "style": "candles"}))
                            ws.send(json.dumps({"ticks_history": symbol, "granularity": 300, "count": 50, "end": "latest", "style": "candles"}))
                        except Exception:
                            pass
                print(Fore.CYAN + "Config reloaded, pairs:", list(CONFIG_PAIRS_MAP.items()))
            except Exception as e:
                print(Fore.RED + "Failed to reload config:", e)
        time.sleep(interval)


# ============================================================
#  BLANK STATE
# ============================================================
def _blank_state():
    return {
        "candles":              [],
        "current":              None,
        "ticks":                [],
        "vol_window":           deque(maxlen=20),
        "price":                0,
        "bot_signal_candidate": None,
        "bot_signal_count":     0,
        "bot_signal":           None,
        "last_signal_minute":   None,   # minute key of last placed trade
        "trades_this_minute":   0,      # count of trades placed in current minute
        "sym_wins":             0,
        "sym_losses":           0,
        "loss_cooldown":        0,
        "ind":                  None,
        "htf_candles":          [],
        "htf_ind":              None,
        "htf_ready":            False,
        "pending_fps":          [],     # list of fingerprints awaiting contract_id
        "mem_tag":              "-",    # dashboard display tag
        # ---- liquidity zone state (updated each tick) ----
        "zone_high":            None,
        "zone_low":             None,
        "zone_approach":        {},
        "zone_patterns":        [],
        "zone_confirming":      [],
        "zone_signal_type":     "WAIT",
        "market_structure":     {},
        "prev_price":           None,
    }


# ============================================================
#  SYMBOL SCORING & AUTO-RESELECTION
# ============================================================

def _score_symbol(symbol):
    """
    Score a symbol from the pool for tradability.
    Higher = better candidate for active trading.
    Criteria (weighted):
      • Tick volatility (ATR)              30%
      • Trend clarity (EMA fast/slow gap)  25%
      • 10-candle tick dominance spread    25%
      • HTF alignment (trend matches)      20%
    Returns float score >= 0.
    """
    s   = symbol_state.get(symbol)
    if not s:
        return 0.0
    ind = s.get("ind") or {}
    htf = s.get("htf_ind") or {}

    # ── ATR score: normalised by price ──────────────────────────────────────
    atr_val = ind.get("atr_val") or 0
    price   = s.get("price") or 1
    atr_score = min(atr_val / (price * 0.01), 1.0) if price > 0 else 0.0   # cap at 1

    # ── EMA gap: |fast - slow| / slow  → trend clarity ──────────────────────
    ef  = ind.get("ema_fast") or 0
    es  = ind.get("ema_slow") or 1
    ema_gap = min(abs(ef - es) / (es if es else 1), 1.0)

    # ── Tick dominance spread (max of buy% and sell%) ─────────────────────────
    ten_buy, ten_sell = calculate_5c(symbol)
    dominance = 0.0
    if ten_buy is not None and ten_sell is not None:
        dominance = max(ten_buy, ten_sell) / 100.0   # 0-1

    # ── HTF alignment bonus ──────────────────────────────────────────────────
    htf_trend = htf.get("htf_trend")
    trend     = ind.get("trend")
    htf_align = 1.0 if (htf_trend and trend and htf_trend == trend) else 0.0

    score = (0.30 * atr_score +
             0.25 * ema_gap   +
             0.25 * dominance +
             0.20 * htf_align)
    return round(score, 4)


def _pick_top_symbols(ws, n=5):
    """
    Score all initialised pool symbols and return the top-N list.
    Subscribes ticks for newly selected symbols and unsubscribes
    symbols that are dropped (if they have no open trades).
    """
    global top_symbols, _last_reselect_ts, _next_reselect_ts

    ready = [sym for sym in SYMBOL_POOL
             if symbol_state.get(sym, {}).get("ready")]
    if not ready:
        return   # candle history not loaded yet — too early to score

    scored = sorted(ready, key=_score_symbol, reverse=True)
    new_top = scored[:n]

    old_set = set(top_symbols)
    new_set = set(new_top)
    added   = new_set - old_set
    removed = old_set - new_set

    # Unsubscribe dropped symbols (only if no open trades)
    for sym in removed:
        open_for_sym = any(
            t.get("status") == "open" and t.get("symbol") == sym
            for t in active_trades.values()
        )
        if not open_for_sym:
            try:
                ws.send(json.dumps({"forget_all": "ticks"}))   # Deriv: forget by category
            except Exception:
                pass

    # Subscribe new symbols
    for sym in added:
        try:
            ws.send(json.dumps({"ticks": sym, "subscribe": 1}))
        except Exception:
            pass

    top_symbols = new_top
    _last_reselect_ts = time.time()
    _next_reselect_ts = _last_reselect_ts + SYMBOL_RESELECT_INTERVAL

    scores_str = "  ".join(
        f"{sym}({_score_symbol(sym):.3f})" for sym in new_top
    )
    print(Fore.CYAN + Style.BRIGHT +
          f"\n[SymPicker] Top-{n} selected: {scores_str}")


def _symbol_reselect_loop(ws):
    """
    Background thread — reselects top symbols every SYMBOL_RESELECT_INTERVAL seconds.
    First call fires immediately once candle histories are loaded.
    """
    # Wait until at least one symbol is ready before first selection
    while not any(symbol_state.get(s, {}).get("ready") for s in SYMBOL_POOL):
        time.sleep(2)
    _pick_top_symbols(ws)

    while True:
        time.sleep(SYMBOL_RESELECT_INTERVAL)
        _pick_top_symbols(ws)


# ============================================================
#  DASHBOARD
# ============================================================
def dashboard_loop():
    while True:
        os.system("cls" if os.name == "nt" else "clear")

        with _memory_lock:
            wc = len(_win_memory)
            lc = len(_loss_memory)

        mode_str = f"Mode: {Fore.YELLOW}{STAKE_MODE.upper()}{Fore.CYAN}"
        # Show balance in both modes — manual balance is kept updated via subscription
        bal_str  = f"Bal: {ACCOUNT_BALANCE:.2f}" if ACCOUNT_BALANCE > 0 else "Bal: --"

        print(Style.BRIGHT + Fore.CYAN + "=" * 175)
        print(Style.BRIGHT + Fore.CYAN +
              f"  DERIV BOT  |  {mode_str}  |  Stake: {STAKE}  |  "
              f"TP: {MAX_TP}  SL: {MAX_SL}  |  {bal_str}  |  " +
              Fore.GREEN + f"Mem✔:{wc}  " + Fore.RED + f"Mem✖:{lc}")

        # ── Symbol picker status line ────────────────────────────────────────
        now_ts_dash = time.time()
        with _reselect_lock:
            nxt = _next_reselect_ts
        reselect_in = max(0, int(nxt - now_ts_dash))
        sym_scores_str = "  ".join(
            Fore.YELLOW + f"{sym}" + Fore.WHITE + f"({_score_symbol(sym):.3f})"
            for sym in top_symbols
        ) if top_symbols else Fore.WHITE + "selecting…"
        print(Fore.CYAN + Style.BRIGHT +
              f"  🔍 AUTO-SELECTED PAIRS [{len(top_symbols)}/5]:  " +
              sym_scores_str +
              Fore.CYAN + f"   ⏱ reselect in {reselect_in}s")
        print(Style.BRIGHT + Fore.CYAN + "=" * 175)
        print(Fore.WHITE + Style.BRIGHT +
              f"{'SYMBOL':<10}{'PRICE':<12}{'10C BUY%':<10}{'10C SELL%':<10}"
              f"{'CURR BUY%':<10}{'DISP':<12}{'TREND':<7}{'EXHST':<7}"
              f"{'HTF':<7}{'HTF RSI':<9}{'HTF EXH':<9}"
              f"{'ZONE HI':<12}{'ZONE LO':<12}{'POS%':<7}{'ZONE_SIG':<18}{'SIGNAL':<8}{'MEM':<8}")
        print(Fore.CYAN + "=" * 175)

        for symbol in top_symbols:
            s   = symbol_state.get(symbol, {})
            ind = s.get("ind") or {}
            htf = s.get("htf_ind") or {}

            trend_val   = ind.get("trend") or "-"
            exhst_val   = "YES" if ind.get("exhausted") else "no"
            htf_trend   = htf.get("htf_trend") or "-"
            htf_rsi_val = htf.get("htf_rsi")
            htf_rsi_str = f"{htf_rsi_val:.1f}" if htf_rsi_val is not None else "-"
            htf_exh_str = "YES" if htf.get("htf_exhausted") else "no"
            sig         = s.get("signal",  "-")
            mem_tag     = s.get("mem_tag", "-")

            # Zone dashboard values
            zh          = s.get("zone_high")
            zl          = s.get("zone_low")
            zh_str      = f"{zh:.4f}" if zh else "-"
            zl_str      = f"{zl:.4f}" if zl else "-"
            pct_pos     = s.get("pct_pos")
            pct_str     = f"{pct_pos*100:.0f}%" if pct_pos is not None else "-"

            approach    = s.get("zone_approach", {})
            confirming  = s.get("zone_confirming", [])
            if approach.get("breached_high"):   zone_tag = "BRCH-HI"
            elif approach.get("breached_low"):  zone_tag = "BRCH-LO"
            elif approach.get("at_high"):       zone_tag = "AT HIGH!"
            elif approach.get("at_low"):        zone_tag = "AT LOW! "
            elif approach.get("approaching_high"): zone_tag = "->HIGH  "
            elif approach.get("approaching_low"):  zone_tag = "->LOW   "
            else:                               zone_tag = "mid-rng "

            if confirming:
                zone_tag += "✔"
                zone_tag_color = Fore.GREEN if approach.get("at_low") or approach.get("approaching_low") else Fore.RED
            elif approach.get("at_high") or approach.get("at_low"):
                zone_tag_color = Fore.YELLOW
            else:
                zone_tag_color = Fore.WHITE

            sig_color   = Fore.GREEN if sig == "BUY" else (Fore.RED if sig == "SELL" else Fore.WHITE)
            trend_color = Fore.GREEN if trend_val == "up" else (Fore.RED if trend_val == "down" else Fore.WHITE)
            mem_color   = (Fore.GREEN  if mem_tag == "BOOST" else
                           Fore.RED    if mem_tag == "BLOCK" else
                           Fore.WHITE)

            print(
                Fore.WHITE  + f"{symbol:<10}" +
                Fore.YELLOW + f"{s.get('price', 0):<12.4f}" +
                Fore.WHITE  + f"{s.get('ten_buy',  0):<10.1f}" +
                Fore.WHITE  + f"{s.get('ten_sell', 0):<10.1f}" +
                Fore.WHITE  + f"{s.get('curr_buy', 0):<10.1f}" +
                Fore.WHITE  + f"{s.get('disp', 0):<12.5f}" +
                trend_color + f"{trend_val:<7}" +
                (Fore.RED if exhst_val == "YES" else Fore.WHITE) + f"{exhst_val:<7}" +
                trend_color + f"{htf_trend:<7}" +
                Fore.WHITE  + f"{htf_rsi_str:<9}" +
                (Fore.RED if htf_exh_str == "YES" else Fore.WHITE) + f"{htf_exh_str:<9}" +
                Fore.CYAN   + f"{zh_str:<12}" +
                Fore.CYAN   + f"{zl_str:<12}" +
                Fore.WHITE  + f"{pct_str:<7}" +
                zone_tag_color + f"{zone_tag:<18}" +
                sig_color   + f"{sig:<8}" +
                mem_color   + f"{mem_tag:<8}"
            )

        print(Fore.CYAN + "=" * 175)
        net       = summary["net_profit"]
        net_color = Fore.GREEN if net >= 0 else Fore.RED
        print(
            Fore.WHITE + f"Trades: {summary['total_trades']}  " +
            Fore.GREEN + f"Wins: {summary['wins']}  " +
            Fore.RED   + f"Losses: {summary['losses']}  " +
            net_color  + f"Net PnL: {net:.2f}"
        )
        print(Fore.CYAN + "=" * 175)

        with active_symbol_lock:
            cur_sym = ACTIVE_SYMBOL
        if cur_sym:
            print(Fore.YELLOW + f"\n  🎯 Currently trading: {cur_sym}")

        open_trades = [(cid, t) for cid, t in active_trades.items() if t.get("status") == "open"]
        if open_trades:
            print(Fore.WHITE + Style.BRIGHT + f"\n  OPEN TRADES ({len(open_trades)})")
            print(Fore.WHITE + f"  {'CONTRACT ID':<16}{'SYMBOL':<10}{'DIR':<8}{'STAKE':<8}{'LIVE P&L':<12}{'AGE (s)':<10}")
            print(Fore.WHITE + f"  {'-'*66}")
            now_ts = int(time.time())
            for cid, t in open_trades:
                age      = now_ts - t.get("bought_at", now_ts)
                live_pnl = t.get("profit", t.get("pnl", "?"))
                pnl_str  = f"{live_pnl:+.2f}" if isinstance(live_pnl, (int, float)) else str(live_pnl)
                sym_t    = t.get("symbol") or "-"
                pnl_color = Fore.GREEN if isinstance(live_pnl, (int, float)) and live_pnl >= 0 else Fore.RED
                print(
                    Fore.WHITE + f"  {str(cid):<16}{sym_t:<10}"
                    + f"{str(t.get('direction','-')):<8}{str(t.get('stake','-')):<8}"
                    + pnl_color + f"{pnl_str:<12}"
                    + Fore.WHITE + f"{age:<10}"
                )
            print()
        else:
            print(Fore.WHITE + "\n  No open trades.\n")

        # ---- PnL Graph ----
        print(Fore.CYAN + Style.BRIGHT + "  ── CUMULATIVE PnL GRAPH (last 60 trades) ──")
        for graph_line in render_pnl_graph(width=60, height=8):
            print(graph_line)
        print(Fore.CYAN + "=" * 175)

        time.sleep(1)


# ============================================================
#  LIQUIDITY ZONE CONFIGURATION  (from n.py)
# ============================================================
LIQ_WINDOW      = 15     # candles to scan for swing high/low
LIQ_APPROACH_AT = 0.5   # % of price to flag "AT ZONE"
LIQ_APPROACH_BAND = 20  # % of range = "approaching" threshold

# ============================================================
#  INDICATORS
# ============================================================
EMA_PERIOD     = 20
ATR_PERIOD     = 14
TREND_EMA_FAST = 20
TREND_EMA_SLOW = 50


def ema(values, period):
    if len(values) < period:
        return []
    k      = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append((v - result[-1]) * k + result[-1])
    return result


def atr(highs, lows, closes, period):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        ))
    result = [None] * (period - 1)
    result.append(sum(trs[:period]) / period)
    for i in range(period, len(trs)):
        result.append((result[-1] * (period - 1) + trs[i]) / period)
    return result


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)
    result = [None] * period
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, period + 1)]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, period + 1)]
    avg_gain = sum(gains)  / period
    avg_loss = sum(losses) / period
    for i in range(period, len(closes)):
        diff     = closes[i] - closes[i-1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0))  / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period
        result.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    return result




# ============================================================
#  LIQUIDITY ZONE ENGINE  (ported from n.py — swing-based)
# ============================================================

def _lz_body(c):   return abs(c["close"] - c["open"])
def _lz_range(c):  return c["high"] - c["low"] if c["high"] != c["low"] else 1e-10
def _lz_uw(c):     return c["high"] - max(c["open"], c["close"])
def _lz_lw(c):     return min(c["open"], c["close"]) - c["low"]
def _lz_bull(c):   return c["close"] > c["open"]
def _lz_bear(c):   return c["close"] < c["open"]


def build_liquidity_zones(candles):
    """
    15-candle lookback liquidity zones.
    Scans the last LIQ_WINDOW (15) fully CLOSED candles (excludes the live
    forming candle) and picks zones independently:
      zone_high = highest wick-high across the 15 candles
      zone_low  = lowest  wick-low  across the 15 candles
    These are always real formed price levels — never the current price.
    Returns (zone_high, zone_low, high_candle, low_candle).
    """
    # Exclude the last (live, incomplete) candle
    closed = candles[:-1]
    if not closed:
        dummy = {"high": 0, "low": 0, "open": 0, "close": 0, "epoch": 0}
        return 0, 0, dummy, dummy

    # Take up to LIQ_WINDOW most recent closed candles
    window = closed[-LIQ_WINDOW:]

    # Highest wick-high — independent scan
    zone_high   = max(c["high"] for c in window)
    high_candle = next(c for c in reversed(window) if c["high"] == zone_high)

    # Lowest wick-low — independent scan
    zone_low   = min(c["low"] for c in window)
    low_candle = next(c for c in reversed(window) if c["low"] == zone_low)

    return zone_high, zone_low, high_candle, low_candle


def get_zone_approach(current, prev_price, zone_high, zone_low):
    """
    Measure price position relative to liquidity zones and direction of travel.
    Returns a rich dict used for signal generation and dashboard display.
    """
    total_rng = zone_high - zone_low
    if total_rng <= 0:
        total_rng = zone_high * 0.001 or 1.0

    dist_high_pct = (zone_high - current) / total_rng * 100
    dist_low_pct  = (current - zone_low)  / total_rng * 100
    pct_pos       = (current - zone_low)  / total_rng
    pct_pos       = max(0.0, min(1.0, pct_pos))

    delta = current - prev_price
    if   delta >  0: momentum = "UP"
    elif delta <  0: momentum = "DOWN"
    else:            momentum = "FLAT"

    approaching_high = (dist_high_pct <= LIQ_APPROACH_BAND and momentum == "UP")
    approaching_low  = (dist_low_pct  <= LIQ_APPROACH_BAND and momentum == "DOWN")

    breached_high = current >= zone_high
    breached_low  = current <= zone_low

    # "AT zone" = within LIQ_APPROACH_AT % of absolute price level
    at_high = ((zone_high - current) / zone_high * 100) <= LIQ_APPROACH_AT if (zone_high > 0 and not breached_high) else breached_high
    at_low  = ((current - zone_low)  / zone_low  * 100) <= LIQ_APPROACH_AT if (zone_low  > 0 and not breached_low)  else breached_low

    return dict(
        dist_high_pct    = dist_high_pct,
        dist_low_pct     = dist_low_pct,
        pct_pos          = pct_pos,
        momentum         = momentum,
        approaching_high = approaching_high,
        approaching_low  = approaching_low,
        breached_high    = breached_high,
        breached_low     = breached_low,
        at_high          = at_high,
        at_low           = at_low,
    )


def detect_reversal_patterns(closed_candles):
    """
    Examine last 1-3 fully closed candles for reversal patterns near a zone.
    Returns list of dicts: [{name, bias, desc}]
      bias = "BULL" | "BEAR"
    (ported directly from n.py)
    """
    patterns = []
    if len(closed_candles) < 2:
        return patterns

    c1 = closed_candles[-1]
    c2 = closed_candles[-2]
    c3 = closed_candles[-3] if len(closed_candles) >= 3 else None

    r1  = _lz_range(c1); b1 = _lz_body(c1); uw1 = _lz_uw(c1); lw1 = _lz_lw(c1)
    b2  = _lz_body(c2)

    # Hammer
    if (_lz_bull(c1) and b1 > 0 and lw1 >= 2 * b1 and uw1 <= 0.4 * b1
            and (c1["close"] - c1["low"]) / r1 >= 0.55):
        patterns.append({"name": "HAMMER", "bias": "BULL",
                          "desc": "Long lower wick — buyers rejected the low"})

    # Shooting Star
    if (_lz_bear(c1) and b1 > 0 and uw1 >= 2 * b1 and lw1 <= 0.4 * b1
            and (c1["high"] - c1["close"]) / r1 >= 0.55):
        patterns.append({"name": "SHOOT STAR", "bias": "BEAR",
                          "desc": "Long upper wick — sellers rejected the high"})

    # Bull Pin Bar
    if lw1 >= 0.60 * r1 and (c1["close"] - c1["low"]) / r1 >= 0.65:
        patterns.append({"name": "BULL PINBAR", "bias": "BULL",
                          "desc": "Lower wick dominates — strong rejection of lows"})

    # Bear Pin Bar
    if uw1 >= 0.60 * r1 and (c1["high"] - c1["close"]) / r1 >= 0.65:
        patterns.append({"name": "BEAR PINBAR", "bias": "BEAR",
                          "desc": "Upper wick dominates — strong rejection of highs"})

    # Doji
    if b1 <= 0.10 * r1:
        if _lz_bear(c2):
            patterns.append({"name": "MORNING DOJI", "bias": "BULL",
                              "desc": "Doji after bearish candle — indecision at low"})
        elif _lz_bull(c2):
            patterns.append({"name": "EVENING DOJI", "bias": "BEAR",
                              "desc": "Doji after bullish candle — indecision at high"})

    # Bullish Engulfing
    if (_lz_bear(c2) and _lz_bull(c1) and b1 > b2
            and c1["open"] <= c2["close"] and c1["close"] >= c2["open"]):
        patterns.append({"name": "BULL ENGULF", "bias": "BULL",
                          "desc": "Bullish candle fully engulfs prior bear — reversal"})

    # Bearish Engulfing
    if (_lz_bull(c2) and _lz_bear(c1) and b1 > b2
            and c1["open"] >= c2["close"] and c1["close"] <= c2["open"]):
        patterns.append({"name": "BEAR ENGULF", "bias": "BEAR",
                          "desc": "Bearish candle fully engulfs prior bull — reversal"})

    # Tweezer Bottom
    if c1["low"] > 0 and abs(c1["low"] - c2["low"]) / c1["low"] <= 0.0005:
        patterns.append({"name": "TWEEZER BOT", "bias": "BULL",
                          "desc": "Equal lows tested twice — strong support confirmed"})

    # Tweezer Top
    if c1["high"] > 0 and abs(c1["high"] - c2["high"]) / c1["high"] <= 0.0005:
        patterns.append({"name": "TWEEZER TOP", "bias": "BEAR",
                          "desc": "Equal highs tested twice — strong resistance confirmed"})

    # Three-candle patterns
    if c3 is not None:
        b3 = _lz_body(c3)
        # Morning Star
        if (_lz_bear(c3) and b2 <= 0.35 * b3 and _lz_bull(c1) and b1 >= 0.5 * b3
                and c1["close"] > (c3["open"] + c3["close"]) / 2):
            patterns.append({"name": "MORNING STAR", "bias": "BULL",
                              "desc": "3-candle bottom reversal — high probability long"})
        # Evening Star
        if (_lz_bull(c3) and b2 <= 0.35 * b3 and _lz_bear(c1) and b1 >= 0.5 * b3
                and c1["close"] < (c3["open"] + c3["close"]) / 2):
            patterns.append({"name": "EVENING STAR", "bias": "BEAR",
                              "desc": "3-candle top reversal — high probability short"})

    return patterns


def analyse_market_structure(candles, window=60):
    """
    Split the window in half, compare HH/HL/LH/LL to determine market structure.
    Returns dict with label and trend string.
    """
    w = candles[-window:] if len(candles) >= window else candles
    if len(w) < 4:
        return {"label": "Insufficient data", "trend": "NEUTRAL"}

    half   = len(w) // 2
    first  = w[:half]
    second = w[half:]

    high1 = max(c["high"] for c in first)
    high2 = max(c["high"] for c in second)
    low1  = min(c["low"]  for c in first)
    low2  = min(c["low"]  for c in second)

    hh = high2 > high1
    lh = high2 < high1
    eh = abs(high2 - high1) / (high1 or 1) < 0.0001
    hl = low2  > low1
    ll = low2  < low1
    el = abs(low2 - low1) / (low1 or 1) < 0.0001

    if hh and hl:    label, trend = "HH+HL (Bull)",    "BULLISH"
    elif lh and ll:  label, trend = "LH+LL (Bear)",    "BEARISH"
    elif hh and ll:  label, trend = "HH+LL (Expand)",  "EXPANDING"
    elif lh and hl:  label, trend = "LH+HL (Cont)",    "CONTRACTING"
    elif eh and el:  label, trend = "EH+EL (Range)",   "RANGING"
    elif eh and hl:  label, trend = "EH+HL (WkBull)",  "WEAK BULLISH"
    elif eh and ll:  label, trend = "EH+LL (WkBear)",  "WEAK BEARISH"
    elif hh and el:  label, trend = "HH+EL (WkBull)",  "WEAK BULLISH"
    elif lh and el:  label, trend = "LH+EL (WkBear)",  "WEAK BEARISH"
    else:            label, trend = "Mixed",            "NEUTRAL"

    return {"label": label, "trend": trend, "high1": high1, "high2": high2,
            "low1": low1, "low2": low2}


def compute_indicators_from_candles(candles):
    if len(candles) < TREND_EMA_SLOW:
        return {}
    closes = [c["close"] for c in candles]
    highs  = [c.get("high", max(c.get("open", 0), c["close"])) for c in candles]
    lows   = [c.get("low",  min(c.get("open", 0), c["close"])) for c in candles]

    ema_fast_vals = ema(closes, TREND_EMA_FAST)
    ema_slow_vals = ema(closes, TREND_EMA_SLOW)
    atr_vals      = atr(highs, lows, closes, ATR_PERIOD)

    ema_fast      = ema_fast_vals[-1] if ema_fast_vals else None
    ema_fast_prev = ema_fast_vals[-2] if len(ema_fast_vals) >= 2 else None
    ema_slow      = ema_slow_vals[-1] if ema_slow_vals else None
    atr_val       = atr_vals[-1] if atr_vals and atr_vals[-1] is not None else None

    trend = None
    if ema_fast and ema_slow:
        trend = "up" if ema_fast > ema_slow else "down"

    EXHAUSTION_LOOKBACK  = 10
    EXHAUSTION_THRESHOLD = 8
    exhausted = False
    if len(candles) >= EXHAUSTION_LOOKBACK:
        last10     = candles[-EXHAUSTION_LOOKBACK:]
        bull_count = sum(1 for c in last10 if c["close"] > c["open"])
        bear_count = sum(1 for c in last10 if c["close"] < c["open"])
        if bull_count >= EXHAUSTION_THRESHOLD or bear_count >= EXHAUSTION_THRESHOLD:
            exhausted = True

    return {
        "ema_fast":      ema_fast,
        "ema_fast_prev": ema_fast_prev,
        "ema_slow":      ema_slow,
        "atr_val":       atr_val,
        "trend":         trend,
        "exhausted":     exhausted,
    }


HTF_GRANULARITY = 300          # 5-minute HTF
HTF_RSI_PERIOD  = 10
HTF_EMA_FAST    = 9
HTF_EMA_SLOW    = 21
HTF_RSI_OB      = 70
HTF_RSI_OS      = 30


def compute_htf_indicators(candles):
    if len(candles) < HTF_EMA_SLOW + 1:
        return {"htf_trend": None, "htf_rsi": None, "htf_exhausted": False}
    closes = [c["close"] for c in candles]

    ema_fast_vals = ema(closes, HTF_EMA_FAST)
    ema_slow_vals = ema(closes, HTF_EMA_SLOW)
    htf_ema_fast  = ema_fast_vals[-1] if ema_fast_vals else None
    htf_ema_slow  = ema_slow_vals[-1] if ema_slow_vals else None

    htf_trend = None
    if htf_ema_fast and htf_ema_slow:
        htf_trend = "up" if htf_ema_fast > htf_ema_slow else "down"

    rsi_window    = closes[-(HTF_RSI_PERIOD + 1):]
    rsi_vals      = rsi(rsi_window, HTF_RSI_PERIOD)
    htf_rsi       = rsi_vals[-1] if rsi_vals and rsi_vals[-1] is not None else None

    htf_exhausted = False
    if htf_rsi is not None and htf_trend is not None:
        if htf_trend == "up"   and htf_rsi >= HTF_RSI_OB:
            htf_exhausted = True
        elif htf_trend == "down" and htf_rsi <= HTF_RSI_OS:
            htf_exhausted = True

    return {"htf_trend": htf_trend, "htf_rsi": htf_rsi, "htf_exhausted": htf_exhausted}


# ============================================================
#  TICK PROCESS
# ============================================================
def process_tick(ws, symbol, price):
    state = symbol_state[symbol]
    state["price"] = price

    now        = int(time.time())
    minute_key = now - (now % 60)

    if state["current"] is None or state["current"]["minute"] != minute_key:
        if state["current"]:
            try:
                ws.send(json.dumps({"ticks_history": symbol, "granularity": 60, "count": 60, "end": "latest", "style": "candles"}))
                if minute_key % HTF_GRANULARITY == 0:
                    ws.send(json.dumps({"ticks_history": symbol, "granularity": HTF_GRANULARITY, "count": 50, "end": "latest", "style": "candles"}))
            except Exception:
                pass
        state["current"] = {
            "minute": minute_key, "open": price, "close": price,
            "high": price, "low": price, "buy": 0, "sell": 0
        }

    candle = state["current"]
    candle["close"] = price
    candle["high"]  = max(candle.get("high", price), price)
    candle["low"]   = min(candle.get("low",  price), price)

    if state["ticks"]:
        prev = state["ticks"][-1]
        if   price > prev: candle["buy"]  += 1
        elif price < prev: candle["sell"] += 1
        state["vol_window"].append(abs(price - prev))

    state["ticks"].append(price)
    analyze(ws, symbol)


# ============================================================
#  ANALYSIS
# ============================================================
def calculate_5c(symbol):
    candles = symbol_state[symbol].get("candles", [])
    if len(candles) < 10:
        return None, None
    last10 = candles[-10:]
    buy    = sum(1 for c in last10 if c["close"] >= c["open"])
    sell   = sum(1 for c in last10 if c["close"] <  c["open"])
    total  = buy + sell
    if total == 0:
        return None, None
    return (buy / total) * 100, (sell / total) * 100


def analyze(ws, symbol):
    global ACTIVE_SYMBOL

    state  = symbol_state[symbol]
    candle = state["current"]
    if not candle:
        return

    total        = candle["buy"] + candle["sell"]
    displacement = candle["close"] - candle["open"]

    ind           = state.get("ind") or {}
    atr_val       = ind.get("atr_val")
    trend         = ind.get("trend")
    exhausted     = ind.get("exhausted", False)

    htf           = state.get("htf_ind") or {}
    htf_trend     = htf.get("htf_trend")
    htf_exhausted = htf.get("htf_exhausted", False)

    curr_buy          = (candle["buy"] / total * 100) if total > 0 else 0
    ten_buy, ten_sell = calculate_5c(symbol)

    disp_threshold = atr_val * 0.3 if (atr_val and atr_val > 0) else 0
    disp_up        = displacement >  disp_threshold
    disp_down      = displacement < -disp_threshold

    state.update({
        "ten_buy":  ten_buy  or 0,
        "ten_sell": ten_sell or 0,
        "curr_buy": curr_buy,
        "disp":     displacement,
        "signal":   "-",
    })

    # ── LIQUIDITY ZONE UPDATE (every tick, independent of trade gate) ──────────
    candles = state.get("candles", [])
    current_price = candle["close"]
    prev_price    = state.get("prev_price") or current_price
    state["prev_price"] = current_price

    if len(candles) >= 3:
        # Build the live candle list including the current forming candle
        live_candles = candles + [{
            "open": candle["open"], "close": candle["close"],
            "high": candle["high"], "low": candle["low"],
            "epoch": int(time.time()),
        }]
        try:
            zone_high, zone_low, _hc, _lc = build_liquidity_zones(live_candles)
            approach = get_zone_approach(current_price, prev_price, zone_high, zone_low)
            structure = analyse_market_structure(candles)

            # Reversal patterns — only scan when near a zone
            near_zone = (approach["at_high"] or approach["at_low"] or
                         approach["approaching_high"] or approach["approaching_low"])
            closed_candles = candles  # these are fully closed
            patterns = detect_reversal_patterns(closed_candles[-3:]) if near_zone and len(closed_candles) >= 2 else []

            # Confirm patterns by expected bias
            if approach["at_low"] or approach["approaching_low"]:
                confirming = [p for p in patterns if p["bias"] == "BULL"]
            elif approach["at_high"] or approach["approaching_high"]:
                confirming = [p for p in patterns if p["bias"] == "BEAR"]
            else:
                confirming = []

            state.update({
                "zone_high":        zone_high,
                "zone_low":         zone_low,
                "zone_approach":    approach,
                "zone_patterns":    patterns,
                "zone_confirming":  confirming,
                "market_structure": structure,
                "dist_high":        approach["dist_high_pct"],
                "dist_low":         approach["dist_low_pct"],
                "pct_pos":          approach["pct_pos"],
                "zone_momentum":    approach["momentum"],
            })
        except Exception as _ze:
            pass

    # ── TRADE GATE checks ──────────────────────────────────────────────────────
    if total < 5:
        return
    if trend is None:
        return

    # HTF exhaustion: only block when BOTH timeframes are exhausted together
    if exhausted and htf_exhausted:
        return

    # HTF trend disagreement: only block when HTF is clearly opposing (not exhausted)
    if htf_trend is not None and htf_trend != trend and not exhausted:
        return

    sym_wins   = state.get("sym_wins", 0)
    sym_losses = state.get("sym_losses", 0)
    sym_total  = sym_wins + sym_losses
    if sym_total >= MIN_TRADES_FOR_WINRATE and (sym_wins / sym_total) < MIN_WIN_RATE:
        return

    if state.get("loss_cooldown", 0) > 0:
        state["loss_cooldown"] -= 1
        return

    if len(candles) >= 2:
        last_close = candles[-1]["close"]
        prev_close = candles[-2]["close"]
        candle_up   = last_close > prev_close
        candle_down = last_close < prev_close
    else:
        candle_up = candle_down = False

    # ── SIGNAL GENERATION ─────────────────────────────────────────────────────
    signal    = "-"
    direction = None

    approach   = state.get("zone_approach", {})
    confirming = state.get("zone_confirming", [])
    is_bull    = trend == "up"
    is_bear    = trend == "down"

    at_low           = approach.get("at_low", False)
    at_high          = approach.get("at_high", False)
    approaching_low  = approach.get("approaching_low", False)
    approaching_high = approach.get("approaching_high", False)
    has_confirm      = len(confirming) > 0

    # ── TIER 1: Zone + pattern + tick dominance (highest confidence) ──────────
    if (at_low and is_bull and has_confirm
            and ten_buy is not None and ten_buy > SIGNAL_THRESHOLD
            and curr_buy > CURR_BUY_THRESHOLD):
        signal    = "BUY"
        direction = "CALL"

    elif (at_high and is_bear and has_confirm
            and ten_sell is not None and ten_sell > SIGNAL_THRESHOLD
            and curr_buy < CURR_SELL_THRESHOLD):
        signal    = "SELL"
        direction = "PUT"

    # ── TIER 2: Approaching zone + displacement + tick dominance ─────────────
    elif (approaching_low and is_bull
            and disp_up
            and ten_buy is not None and ten_buy > SIGNAL_THRESHOLD
            and curr_buy > CURR_BUY_THRESHOLD
            and candle_up):
        signal    = "BUY"
        direction = "CALL"

    elif (approaching_high and is_bear
            and disp_down
            and ten_sell is not None and ten_sell > SIGNAL_THRESHOLD
            and curr_buy < CURR_SELL_THRESHOLD
            and candle_down):
        signal    = "SELL"
        direction = "PUT"

    # ── TIER 3: Tick dominance + displacement + trend (always active) ─────────
    elif (is_bull and ten_buy is not None and ten_buy > SIGNAL_THRESHOLD
            and disp_up and curr_buy > CURR_BUY_THRESHOLD and candle_up):
        signal    = "BUY"
        direction = "CALL"

    elif (is_bear and ten_sell is not None and ten_sell > SIGNAL_THRESHOLD
            and disp_down and curr_buy < CURR_SELL_THRESHOLD and candle_down):
        signal    = "SELL"
        direction = "PUT"

    state["signal"] = signal
    if signal == "-":
        state["mem_tag"] = "-"
        return

    curr_minute = candle.get("minute")
    if curr_minute is None:
        return

    # Reset per-minute counter when a new candle minute starts
    if state.get("last_signal_minute") != curr_minute:
        state["trades_this_minute"] = 0

    # ---- SLOT GATE — respect MAX_SYMBOLS and MAX_OPEN_TRADES ----
    with active_symbol_lock:
        # Count confirmed open trades for THIS symbol
        sym_open = sum(
            1 for t in active_trades.values()
            if t.get("status") == "open" and t.get("symbol") == symbol
        )
        # Count in-flight proposals for THIS symbol (sent but not yet confirmed open)
        in_flight = len(state.get("pending_fps", []))
        total_committed = sym_open + in_flight

        # Hard cap: never exceed MAX_OPEN_TRADES for this symbol
        if total_committed >= MAX_OPEN_TRADES:
            return

        # If this symbol is not yet active, check global symbol cap
        if symbol not in ACTIVE_SYMBOLS:
            if len(ACTIVE_SYMBOLS) >= MAX_SYMBOLS:
                return
            ACTIVE_SYMBOLS.add(symbol)

    if STAKE <= 0:
        print(Fore.YELLOW + "Stake not yet set. Skipping trade.")
        return

    # ---- PNL SLEEP GATE ----
    if _trading_paused_until > time.time():
        rem = int(_trading_paused_until - time.time())
        print(Fore.YELLOW + f"[Sleep] New trade blocked — paused for {rem}s more.")
        return

    # ---- BUILD FINGERPRINT ----
    fingerprint = build_setup_fingerprint(
        symbol       = symbol,
        direction    = direction,
        candles      = candles,
        ind          = ind,
        htf_ind      = htf,
        ten_buy      = ten_buy,
        ten_sell     = ten_sell,
        curr_buy     = curr_buy,
        displacement = displacement,
        atr_val      = atr_val,
    )

    # ---- MEMORY CHECK (non-blocking, pure RAM) ----
    mem_action, mem_score, _ = check_setup_against_memory(fingerprint)
    state["mem_tag"] = mem_action.upper() if mem_action != "neutral" else "-"

    if mem_action == "block":
        print(Fore.RED + Style.BRIGHT +
              f"[Memory] BLOCKED {signal} on {symbol} "
              f"— matches known loss setup (score={mem_score:.2f})")
        return

    if mem_action == "boost":
        print(Fore.GREEN + Style.BRIGHT +
              f"[Memory] BOOSTED {signal} on {symbol} "
              f"— matches known winning setup (score={mem_score:.2f}). Proceeding.")
    else:
        print(Fore.CYAN + Style.BRIGHT +
              f"Signal {signal} | {symbol} | trend={trend} | htf={htf_trend} | "
              f"htf_rsi={htf.get('htf_rsi', '-')} | 10c={ten_buy:.1f}/{ten_sell:.1f} | "
              f"curr={curr_buy:.1f} | disp={displacement:.5f}")

    # Queue fingerprint — use a list so multiple in-flight proposals don't overwrite each other
    state.setdefault("pending_fps", []).append(fingerprint)

    try:
        ws.send(json.dumps({
            "proposal":      1,
            "amount":        STAKE,
            "basis":         "stake",
            "contract_type": direction,
            "currency":      CURRENCY,
            "duration":      DURATION,
            "duration_unit": "m",
            "symbol":        symbol
        }))
        state["last_signal_minute"] = curr_minute
    except Exception:
        # Roll back the fingerprint if send failed
        if fingerprint in state.get("pending_fps", []):
            state["pending_fps"].remove(fingerprint)
        print(Fore.RED + f"Failed to send proposal for {symbol} {direction}")


# ============================================================
#  TRADE CLOSE — MEMORY SAVE
# ============================================================
def record_trade_close(ws, contract):
    global ACTIVE_SYMBOL

    contract_id = contract.get("contract_id")
    if not contract_id:
        return
    if contract_id in processed_contracts:
        return
    processed_contracts.add(contract_id)

    try:
        profit = float(contract.get("profit") or 0)
    except Exception:
        profit = 0.0

    close_time    = int(time.time())
    won           = profit > 0
    t             = active_trades.get(contract_id, {})
    closed_symbol = t.get("symbol")

    # ---- Retrieve & save the fingerprint ----
    # Strategy 1: contract_id keyed dict (set in buy handler)
    fingerprint = pending_fingerprints.pop(contract_id, None)

    # Strategy 2: pop from symbol state pending_fps list
    if fingerprint is None and closed_symbol and closed_symbol in symbol_state:
        fps_list = symbol_state[closed_symbol].get("pending_fps", [])
        if fps_list:
            fingerprint = fps_list.pop(0)

    # Strategy 3: last-resort — build a minimal fingerprint from active_trades data
    # so we always write something to win/loss memory even if the chain broke
    if fingerprint is None:
        sym_state = symbol_state.get(closed_symbol, {})
        ind_snap  = sym_state.get("ind") or {}
        htf_snap  = sym_state.get("htf_ind") or {}
        candles   = sym_state.get("candles", [])
        atr_val   = ind_snap.get("atr_val") or 1
        displacement = (candles[-1]["close"] - candles[-1]["open"]) if candles else 0
        ten_buy, ten_sell = calculate_5c(closed_symbol) if closed_symbol else (None, None)
        fingerprint = build_setup_fingerprint(
            symbol       = closed_symbol or "-",
            direction    = t.get("direction") or "-",
            candles      = candles,
            ind          = ind_snap,
            htf_ind      = htf_snap,
            ten_buy      = ten_buy,
            ten_sell     = ten_sell,
            curr_buy     = sym_state.get("curr_buy", 0),
            displacement = displacement,
            atr_val      = atr_val,
        )
        fingerprint["_source"] = "fallback"
        print(Fore.YELLOW + f"[Memory] Used fallback fingerprint for contract {contract_id}")

    fingerprint["pnl"]       = profit
    fingerprint["won"]       = won
    fingerprint["closed_at"] = close_time
    save_trade_memory(fingerprint, won)
    tag = Fore.GREEN + "✔ WIN " if won else Fore.RED + "✖ LOSS"
    print(tag + Fore.WHITE +
          f" | Memory saved → {'win.json' if won else 'loss.json'} "
          f"| symbol={closed_symbol} | pnl={profit:+.2f}")

    # ---- Summary ----
    entry = {
        "contract_id":      contract_id,
        "pnl":              profit,
        "closed_at":        close_time,
        "won":              won,
        "symbol":           closed_symbol,
        "direction":        t.get("direction"),
        "stake":            t.get("stake"),
        "bought_at":        t.get("bought_at"),
        "sell_transaction": contract,
    }
    summary["total_trades"] += 1
    summary["net_profit"]   += profit
    if won:
        summary["wins"]   += 1
    else:
        summary["losses"] += 1
    summary.setdefault("trades", []).append(entry)
    save_summary()

    # Update symbol counters and remove the trade FIRST, then check release
    sym = active_trades.get(contract_id, {}).get("symbol")
    if sym and sym in symbol_state:
        if won:
            symbol_state[sym]["sym_wins"]      = symbol_state[sym].get("sym_wins", 0) + 1
        else:
            symbol_state[sym]["sym_losses"]    = symbol_state[sym].get("sym_losses", 0) + 1
            symbol_state[sym]["loss_cooldown"] = LOSS_COOLDOWN_TICKS
        # Don't wipe pending_fps — other in-flight trades may still need it

    # Hard-delete the trade NOW so the ACTIVE_SYMBOLS check below sees correct counts
    active_trades.pop(contract_id, None)
    save_open_trades()   # persist the removal

    # Release symbol from ACTIVE_SYMBOLS when no remaining open trades AND no in-flight proposals
    with active_symbol_lock:
        if closed_symbol and closed_symbol in ACTIVE_SYMBOLS:
            still_open = any(
                t2.get("status") == "open" and t2.get("symbol") == closed_symbol
                for t2 in active_trades.values()   # contract_id already removed above
            )
            in_flight = len(
                symbol_state.get(closed_symbol, {}).get("pending_fps", [])
            )
            if not still_open and in_flight == 0:
                ACTIVE_SYMBOLS.discard(closed_symbol)
                print(Fore.CYAN + f"[Slot] Released symbol slot: {closed_symbol} "
                      f"| Active now: {sorted(ACTIVE_SYMBOLS) or 'none'}")

    # ---- Refresh account balance after every trade close ----
    # The balance subscription will push an update automatically, but we
    # also request one explicitly to guarantee it arrives promptly.
    try:
        _ws_ref = ws   # ws is the param passed into record_trade_close
        _ws_ref.send(json.dumps({"balance": 1}))
    except Exception:
        pass

    # ---- PnL graph point ----
    record_pnl_point(summary["net_profit"])
    _check_pnl_sleep()

    # ---- Daily TP / SL check ----
    net = summary["net_profit"]
    try:
        if MAX_TP > 0 and net >= MAX_TP:
            print(Fore.GREEN + Style.BRIGHT +
                  f"[Daily] TP reached: {net:+.2f} >= {MAX_TP}")
            shutdown(ws, reason=f"Daily TP hit ({net:+.2f})")
        elif MAX_SL > 0 and net <= -abs(MAX_SL):
            print(Fore.RED + Style.BRIGHT +
                  f"[Daily] SL hit: {net:+.2f} <= -{abs(MAX_SL)}")
            shutdown(ws, reason=f"Daily SL hit ({net:+.2f})")
    except SystemExit:
        raise
    except Exception as e:
        print(Fore.RED + "Error checking daily TP/SL:", e)


# ============================================================
#  PNL SLEEP CHECK
# ============================================================
def _check_pnl_sleep():
    """
    If session PnL has moved by PNL_SLEEP_RATIO of the daily TP or SL,
    pause new trade entries for PNL_SLEEP_SECONDS.
    Called after every trade close. Connection stays alive during sleep.
    """
    global _trading_paused_until
    if _trading_paused_until > time.time():
        return   # already sleeping

    net = summary["net_profit"]
    tp  = MAX_TP if MAX_TP > 0 else None
    sl  = MAX_SL if MAX_SL > 0 else None

    triggered = False
    if tp and net >= tp * PNL_SLEEP_RATIO:
        triggered = True
        direction_str = f"profit {net:+.2f} ≥ {PNL_SLEEP_RATIO*100:.0f}% of daily TP ({tp})"
    elif sl and net <= -(sl * PNL_SLEEP_RATIO):
        triggered = True
        direction_str = f"loss {net:+.2f} ≤ -{PNL_SLEEP_RATIO*100:.0f}% of daily SL ({sl})"

    if triggered:
        _trading_paused_until = time.time() + PNL_SLEEP_SECONDS
        print(Fore.YELLOW + Style.BRIGHT +
              f"\n[Sleep] Trading paused {PNL_SLEEP_SECONDS}s — {direction_str}")


# ============================================================
#  WEBSOCKET HANDLERS
# ============================================================
def on_message(ws, message):
    global symbols_loaded, history_count, top_symbols, STAKE, MAX_TP, MAX_SL, ACCOUNT_BALANCE
    global _last_pong_ts
    _last_pong_ts = time.time()   # any server message = connection is alive

    try:
        data = json.loads(message)
    except Exception:
        return

    if "error" in data:
        err  = data.get("error")
        msg  = str(err.get("message", "") if isinstance(err, dict) else err).lower()
        code = str(err.get("code", "")    if isinstance(err, dict) else "").lower()
        is_rate_limit = (
            ("rate" in msg and "limit" in msg) or "throttle" in msg or
            "rate_limit" in code or "429" in code or "too many requests" in msg
        )
        if is_rate_limit:
            print(Fore.YELLOW + "Rate limit received. Sleeping 10 seconds...")
            time.sleep(10)
            return
        print(Fore.RED + "Error:", data.get("error"))
        return

    # Balance update — fires on every balance push (both modes)
    if "balance" in data:
        raw_bal = data["balance"].get("balance", 0)
        if raw_bal and float(raw_bal) > 0:
            new_bal = float(raw_bal)
            if STAKE_MODE == "auto" and STAKE == 0.0:
                # First time: set all auto-stake values
                apply_auto_stake(new_bal)
            else:
                # All other cases (manual OR auto after first set):
                # just update the displayed balance — never change STAKE/TP/SL mid-session
                ACCOUNT_BALANCE = new_bal

    if "authorize" in data:
        auth_data = data.get("authorize", {})
        bal = auth_data.get("balance", 0)
        if STAKE_MODE == "auto":
            if bal and float(bal) > 0 and STAKE == 0.0:
                apply_auto_stake(float(bal))
        elif STAKE_MODE == "manual" and bal and float(bal) > 0:
            # Populate balance display for manual mode too
            ACCOUNT_BALANCE = float(bal)
        # Always subscribe to balance stream — both modes benefit from live updates
        try:
            ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        except Exception:
            pass

        threading.Thread(target=watch_config_loop, args=(ws,), daemon=True).start()

        # ---- Re-subscribe any open trades restored from disk ----
        open_ids = [cid for cid, t in active_trades.items() if t.get("status") == "open"]
        if open_ids:
            print(Fore.CYAN + Style.BRIGHT +
                  f"[Reconnect] Re-subscribing {len(open_ids)} open contract(s): {open_ids}")
            for cid in open_ids:
                try:
                    ws.send(json.dumps({
                        "proposal_open_contract": 1,
                        "contract_id": cid,
                        "subscribe": 1,
                    }))
                except Exception as e:
                    print(Fore.RED + f"[Reconnect] Failed to re-subscribe contract {cid}: {e}")

        # ── Subscribe candle history for every symbol in the pool ──────────
        # The symbol-picker thread will then score them and select the top 5.
        print(Fore.CYAN + Style.BRIGHT +
              f"[SymPicker] Loading history for {len(SYMBOL_POOL)} pool symbols…")
        for symbol in SYMBOL_POOL:
            if symbol not in symbol_state:
                symbol_state[symbol] = _blank_state()
            try:
                ws.send(json.dumps({"ticks_history": symbol, "granularity": 60,
                                    "count": 60, "end": "latest", "style": "candles"}))
                ws.send(json.dumps({"ticks_history": symbol, "granularity": HTF_GRANULARITY,
                                    "count": 50, "end": "latest", "style": "candles"}))
            except Exception:
                pass

        # Launch auto-reselect thread
        threading.Thread(target=_symbol_reselect_loop, args=(ws,),
                         daemon=True, name="sym-picker").start()

        threading.Thread(target=contract_monitor_loop, args=(ws,), daemon=True).start()

    elif "candles" in data:
        symbol = data["echo_req"].get("ticks_history") if "echo_req" in data else None
        if not symbol:
            return
        if symbol not in symbol_state:
            symbol_state[symbol] = _blank_state()

        candle_list = [{
            "minute": c["epoch"],
            "open":   float(c["open"]),
            "close":  float(c["close"]),
            "high":   float(c.get("high", max(c["open"], c["close"]))),
            "low":    float(c.get("low",  min(c["open"], c["close"]))),
        } for c in data["candles"]]

        granularity = data.get("echo_req", {}).get("granularity", 60)

        if granularity == HTF_GRANULARITY:
            symbol_state[symbol]["htf_candles"] = candle_list
            symbol_state[symbol]["htf_ind"]     = compute_htf_indicators(candle_list)
            symbol_state[symbol]["htf_ready"]   = True
            return

        symbol_state[symbol]["candles"] = candle_list
        symbol_state[symbol]["ind"]     = compute_indicators_from_candles(candle_list)

        if not symbol_state[symbol].get("ready"):
            symbol_state[symbol]["ready"] = True
            history_count += 1
            # Once all pool symbols have 1-min candles, launch dashboard
            if history_count >= len(SYMBOL_POOL):
                threading.Thread(target=dashboard_loop, daemon=True).start()

    elif "tick" in data:
        symbol = data["tick"]["symbol"]
        price  = float(data["tick"]["quote"])
        if symbol in top_symbols:
            process_tick(ws, symbol, price)

    elif "proposal" in data:
        prop    = data["proposal"]
        prop_id = prop.get("id")
        # echo_req lives at top-level on Deriv responses, not nested inside proposal
        echo    = data.get("echo_req") or prop.get("echo_req") or {}
        if prop_id:
            proposals[prop_id] = echo   # stores symbol, contract_type, etc.

            # Bridge fingerprint: find the symbol from echo and grab the latest
            # pending fingerprint so we can map prop_id -> fp right now.
            # This is the most reliable point — symbol is definitely known here.
            p_symbol = echo.get("symbol")
            if p_symbol and p_symbol in symbol_state:
                fps_list = symbol_state[p_symbol].get("pending_fps", [])
                if fps_list:
                    # Peek at the last appended fingerprint (LIFO match with this proposal)
                    proposal_fps[str(prop_id)] = fps_list[-1]

        try:
            ws.send(json.dumps({"buy": prop_id, "price": STAKE}))
        except Exception:
            pass

    elif "buy" in data:
        b           = data["buy"]
        contract_id = b.get("contract_id")
        buy_time    = int(time.time())

        # echo_req can live at top-level or inside buy object
        echo = data.get("echo_req") or b.get("echo_req") or {}

        # proposal_id: the buy echo_req has "buy" = prop_id string
        proposal_id = echo.get("buy") or b.get("proposal_id") or None

        # Resolve symbol + direction — try echo first, then stored proposals dict
        symbol    = echo.get("symbol")
        direction = echo.get("contract_type") or echo.get("contract")
        if (not symbol or not direction) and proposal_id:
            prop_echo = proposals.get(str(proposal_id), {})
            symbol    = symbol    or prop_echo.get("symbol")
            direction = direction or prop_echo.get("contract_type") or prop_echo.get("contract")

        if contract_id:
            if contract_id not in active_trades:
                active_trades[contract_id] = {
                    "symbol":    symbol or "-",
                    "direction": direction,
                    "stake":     STAKE,
                    "bought_at": buy_time,
                    "status":    "open",
                    "pnl":       None,
                }
            else:
                active_trades[contract_id].update({
                    "symbol":    symbol or active_trades[contract_id].get("symbol", "-"),
                    "direction": direction or active_trades[contract_id].get("direction"),
                    "stake":     STAKE,
                    "bought_at": buy_time,
                    "status":    "open",
                })

            # ---- Transfer fingerprint to contract_id keyed dict ----
            # Strategy 1: look up by prop_id in proposal_fps (set in proposal handler)
            fp = None
            if proposal_id and str(proposal_id) in proposal_fps:
                fp = proposal_fps.pop(str(proposal_id))
            # Strategy 2: fall back to symbol state pending_fps list
            if fp is None and symbol and symbol in symbol_state:
                fps_list = symbol_state[symbol].get("pending_fps", [])
                if fps_list:
                    fp = fps_list.pop(0)
            if fp is not None:
                pending_fingerprints[contract_id] = fp

            try:
                ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
            except Exception:
                pass

            save_open_trades()   # persist immediately so reconnect can recover this trade
            print(Fore.GREEN + Style.BRIGHT +
                  f"\n✔ Trade Opened | ID: {contract_id} | Symbol: {symbol} | "
                  f"Dir: {direction} | Stake: {STAKE} | Time: {buy_time}")

    elif "proposal_open_contract" in data:
        contract    = data["proposal_open_contract"]
        contract_id = contract.get("contract_id")
        if contract_id:
            # Only update live P&L if trade is still tracked as open
            if contract_id in active_trades and active_trades[contract_id].get("status") == "open":
                active_trades[contract_id].update({
                    "contract": contract,
                    "is_sold":  contract.get("is_sold", False),
                    "profit":   float(contract.get("profit", 0) or 0),
                })

            # Only fire close logic once — processed_contracts is the gate
            if contract.get("is_sold") and contract_id not in processed_contracts:
                profit    = contract.get("profit", 0)
                pnl_color = Fore.GREEN if profit >= 0 else Fore.RED
                print(pnl_color + Style.BRIGHT +
                      f"\n{chr(10004) if profit >= 0 else chr(10006)} Trade Closed | "
                      f"ID: {contract_id} | PnL: {profit:+.2f}")
                record_trade_close(ws, contract)


def on_open(ws):
    global _last_pong_ts, _reconnect_delay
    _last_pong_ts   = time.time()
    _reconnect_delay = RECONNECT_DELAY_MIN   # reset backoff on successful connect
    print(Fore.CYAN + Style.BRIGHT + "WebSocket connected. Authorising...")
    ws.send(json.dumps({"authorize": API_TOKEN}))
    start_ping(ws)


def on_error(ws, error):
    print(Fore.RED + Style.BRIGHT + f"\n[WS] Error: {error}")


def on_close(ws, close_status_code, close_msg):
    print(Fore.YELLOW + Style.BRIGHT +
          f"\n[WS] Connection closed  (code={close_status_code}  msg={close_msg})")


def _reset_session_state():
    """
    Clear per-session volatile state so a fresh connection starts cleanly.
    Preserves: summary, STAKE/TP/SL, memory, PNL_HISTORY, processed_contracts.
    Resets:    symbol subscriptions, proposals, fingerprints, ACTIVE_SYMBOLS.
    Restores:  open trades from disk so reconnect can re-subscribe them.
    """
    global symbols, symbol_state, top_symbols, symbols_loaded, history_count
    global active_trades, proposals, pending_fingerprints, proposal_fps
    global ACTIVE_SYMBOL, ACTIVE_SYMBOLS

    symbols              = []
    symbol_state         = {}
    top_symbols          = []
    symbols_loaded       = False
    history_count        = 0
    proposals            = {}
    pending_fingerprints = {}
    proposal_fps         = {}

    # Pre-create blank state for every pool symbol so scoring can start immediately
    for sym in SYMBOL_POOL:
        symbol_state[sym] = _blank_state()

    # Restore open trades from disk — do NOT wipe them
    active_trades = {}
    load_open_trades()

    with active_symbol_lock:
        ACTIVE_SYMBOL  = None
        # Re-populate ACTIVE_SYMBOLS from any restored open trades
        ACTIVE_SYMBOLS = {t.get("symbol") for t in active_trades.values()
                          if t.get("status") == "open" and t.get("symbol")}
        ACTIVE_SYMBOLS.discard(None)
        ACTIVE_SYMBOLS.discard("-")

    print(Fore.CYAN + "[Reconnect] Session state reset. Re-subscribing symbols...")


def _watchdog_loop(ws_getter):
    """
    Background thread — monitors liveness via _last_pong_ts.
    Closes the socket if no server message arrives within PING_TIMEOUT_SECS,
    which triggers the reconnect loop.
    """
    global _last_pong_ts
    while True:
        time.sleep(5)
        if _last_pong_ts == 0:
            continue
        silence = time.time() - _last_pong_ts
        if silence > PING_TIMEOUT_SECS:
            print(Fore.YELLOW + Style.BRIGHT +
                  f"\n[Watchdog] No server response for {silence:.0f}s — forcing reconnect.")
            try:
                ws = ws_getter()
                if ws:
                    ws.close()
            except Exception:
                pass
            _last_pong_ts = 0   # prevent repeated triggers
            return              # thread exits; reconnect loop spawns a new watchdog


if __name__ == "__main__":
    attempt = 0
    _current_ws = None   # mutable ref so watchdog can close it

    while True:
        attempt += 1
        print(Fore.CYAN + Style.BRIGHT +
              f"\n[Reconnect] Connecting (attempt #{attempt})...")

        _reset_session_state()
        _last_pong_ts = 0

        ws = websocket.WebSocketApp(
            ws_url,
            on_open    = on_open,
            on_message = on_message,
            on_error   = on_error,
            on_close   = on_close,
        )
        _current_ws = ws

        # Start watchdog for this connection
        threading.Thread(
            target  = _watchdog_loop,
            args    = (lambda: _current_ws,),
            daemon  = True,
        ).start()

        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(Fore.RED + f"[Reconnect] run_forever raised: {e}")

        # If we reach here the connection dropped (not a deliberate shutdown)
        print(Fore.YELLOW + Style.BRIGHT +
              f"[Reconnect] Disconnected. Retrying in {_reconnect_delay}s...")
        time.sleep(_reconnect_delay)

        # Exponential backoff
        _reconnect_delay = min(_reconnect_delay * 2, RECONNECT_DELAY_MAX)
