#!/usr/bin/env python3
"""
moon.py

Connects to Deriv WebSocket, requests 100 1-minute candles per target symbol,
computes EMA(20), ATR(14) and an exhaustion metric, derives a trend based on the
previous closed candle vs EMA (one candle back), detects continuation signals,
collects strength metrics, writes top-5 trending pairs to config.json every cycle,
and repeats the cycle every 5 minutes. Keeps the socket alive by pinging every 10s.

When writing config.json the script:
 - Filters excluded markets/symbols
 - Prefers entries that have a continuation signal when picking the top 5
 - For each pair uses the continuation signal (if present) as the "trend" value,
   otherwise uses the computed trend ("uptrend"/"downtrend"/"sideways")

Config file format (unchanged simple format):
{
  "pairs": [
    {"symbol": "...", "trend": "..."},
    ...
  ]
}
"""
import websocket
import json
import time
import threading
import ssl
import os
import subprocess
import sys
import errno
import signal

# Optional: psutil used to detect running processes more reliably
try:
    import psutil
except Exception:
    psutil = None

# Deriv WebSocket endpoint
WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

# Settings
SYMBOL_LIMIT = None            # set to int to limit number of symbols processed
CANDLE_COUNT = 100
GRANULARITY = 60  # seconds (1 minute)
EMA_PERIOD = 20
ATR_PERIOD = 14
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70      # uptrend exhaustion threshold
RSI_OVERSOLD   = 30      # downtrend exhaustion threshold
RSI_EXTREME_OB = 80      # extreme overbought — suppress buy signals
RSI_EXTREME_OS = 20      # extreme oversold  — suppress sell signals
UPDATE_INTERVAL = 2 * 60  # 5 minutes
PING_INTERVAL = 10       # seconds (keepalive)

# Bot call configuration
BOT_SCRIPT = "5min_tbot.py"        # path to bot script
CALL_BOT_ON_UPDATE = True
CALL_BOT_ON_EACH_SYMBOL = False

# Per-user sandboxing — injected by front.py via environment variables.
# TBOT_USER  = stable license name  → persistent files survive reconnects
# TBOT_SID   = socket connection ID → session-scoped temp files
_SID         = os.environ.get("TBOT_SID",  "default")
_USER        = os.environ.get("TBOT_USER", _SID)
# TBOT_DATA_DIR env var redirects config and pid files to a persistent disk.
_BASE_DIR    = os.environ.get("TBOT_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
BOT_PIDFILE  = os.environ.get("TBOT_PID_FILE", os.path.join(_BASE_DIR, f"bot_{_SID}.pid"))
_CONFIG_PATH = os.environ.get("TBOT_CONFIG",   os.path.join(_BASE_DIR, f"config_{_SID}.json"))

# Global state
pending_requests = {}
req_counter = 1
req_lock = threading.Lock()

total_requests = 0
total_requests_lock = threading.Lock()

collected_trends = []  # entries: {symbol, symbol_norm, market, trend, strength, signal, rsi, rsi_exhausted}
collected_lock = threading.Lock()

current_cycle = 0
cycle_lock = threading.Lock()

stop_event = threading.Event()

# Exclusions / inclusions
EXCLUDE_MARKETS = {"forex", "cryptocurrencies", "commodities"}
EXCLUDE_SYMBOLS = {s.upper().strip() for s in {
    "frxAUDUSD","cryBTCUSD","BOOM300N","BOOM500","BOOM600","BOOM900","BOOM1000",
    "CRASH300N","CRASH500","CRASH600","CRASH900","CRASH1000","cryETHUSD",
    "frxGBPUSD","frxEURUSD","frxNZDUSD","frxXPDUSD","frxXPTUSD","frxXAGUSD",
    "WLDUSD","frxUSDCAD","frxUSDCHF","frxUSDJPY","frxUSDMXN","frxUSDNOK",
    "frxUSDPLN","frxUSDSEK",
}}
INCLUDE_SYMBOLS = {s.upper().strip() for s in {
    "stpRNG2","stpRNG3","stpRNG4","stpRNG5","1HZ10V","R_10","1HZ15V","1HZ25V",
    "R_25","1HZ30V","1HZ50V","R_50","1HZ75V","R_75","1HZ90V","1HZ100V","R_100",
    "JD10","JD25","JD50","JD75","JD100",
}}
EXCLUDE_SUBSTRINGS = {"BOOM", "CRASH"}
EXCLUDE_PREFIXES = {"FRX", "CRY"}

# Tracking launched bot processes we started (so we can terminate them on shutdown)
launched_procs = []
launched_lock = threading.Lock()
bot_launch_lock = threading.Lock()

# -------------------------
# Utility helpers
# -------------------------
def next_req_id():
    global req_counter
    with req_lock:
        rid = req_counter
        req_counter += 1
    return rid

def calculate_ema(prices, period):
    if not prices:
        return []
    alpha = 2 / (period + 1)
    ema_values = []
    ema = prices[0]
    ema_values.append(ema)
    for price in prices[1:]:
        ema = (price * alpha) + (ema * (1 - alpha))
        ema_values.append(ema)
    return ema_values

def calculate_atr(highs, lows, closes, period):
    if not closes or not highs or not lows or len(closes) != len(highs) or len(closes) != len(lows):
        return []
    tr_values = []
    for i in range(len(closes)):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i - 1])
            tr3 = abs(lows[i] - closes[i - 1])
            tr = max(tr1, tr2, tr3)
        tr_values.append(tr)
    atr_values = []
    for i in range(len(tr_values)):
        if i < period:
            atr_values.append(None)
        else:
            atr = sum(tr_values[i - period + 1: i + 1]) / period
            atr_values.append(atr)
    return atr_values

def calculate_rsi(closes, period=14):
    """
    Compute RSI using Wilder's smoothed average method.
    Returns a list of RSI values (None for indices where RSI cannot be computed).
    """
    if len(closes) < period + 1:
        return [None] * len(closes)
    rsi_values = [None] * period
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period, len(closes)):
        if i > period:
            delta = closes[i] - closes[i - 1]
            gain = max(delta, 0)
            loss = max(-delta, 0)
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - (100.0 / (1.0 + rs)))
    return rsi_values

def _normalize_symbol_in_obj(symbol_obj):
    sym_raw = (symbol_obj.get("symbol") or "").strip()
    sym_norm = sym_raw.upper()
    market_lower = (symbol_obj.get("market_display_name") or symbol_obj.get("market") or "").strip().lower()
    return sym_raw, sym_norm, market_lower

def is_target_symbol(symbol_obj):
    # permissive request filter; final output filtered strictly
    sym_raw, sym_norm, market_lower = _normalize_symbol_in_obj(symbol_obj)
    if sym_norm in INCLUDE_SYMBOLS:
        return True
    if sym_norm in EXCLUDE_SYMBOLS:
        return False
    if market_lower in EXCLUDE_MARKETS:
        return False
    derived_flag_keys = ["derived", "is_derived", "is_synthetic", "is_symbol_derived"]
    derived = any(bool(symbol_obj.get(k)) for k in derived_flag_keys if k in symbol_obj)
    if sym_raw.startswith("R_"):
        return True
    if derived:
        return True
    if "boom" in sym_raw.lower() or "crash" in sym_raw.lower():
        return True
    if "usd" in sym_norm:
        return True
    return False

def try_extract_candles(data):
    if "candles" in data:
        c = data["candles"]
        if isinstance(c, dict) and "candles" in c:
            return c["candles"]
        if isinstance(c, list):
            return c
    return None

def normalize_req_id(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        try:
            return int(value.strip())
        except Exception:
            return None
    return None

def pop_meta_by_symbol(symbol, cycle):
    sym_norm = (symbol or "").strip().upper()
    for k, v in list(pending_requests.items()):
        if v.get("cycle") != cycle:
            continue
        if v.get("symbol_norm") == sym_norm:
            return pending_requests.pop(k)
    return None

def _symbol_disallowed_by_substring_or_prefix(symbol_norm):
    if not symbol_norm:
        return False
    for pref in EXCLUDE_PREFIXES:
        if symbol_norm.startswith(pref):
            return True
    for sub in EXCLUDE_SUBSTRINGS:
        if sub in symbol_norm:
            return True
    return False

def _allowed_for_output(symbol_norm, market_lower):
    symbol_norm = (symbol_norm or "").strip().upper()
    market_lower = (market_lower or "").strip().lower()
    if symbol_norm in INCLUDE_SYMBOLS:
        return True
    if symbol_norm in EXCLUDE_SYMBOLS:
        return False
    if market_lower in EXCLUDE_MARKETS:
        return False
    if _symbol_disallowed_by_substring_or_prefix(symbol_norm):
        return False
    return True

# -------------------------
# Bot launching / pidfile / cleanup
# -------------------------
def _read_pidfile():
    try:
        if not os.path.isfile(BOT_PIDFILE):
            return None
        with open(BOT_PIDFILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return None
            return int(content.strip())
    except Exception:
        return None

def _write_pidfile(pid):
    try:
        with open(BOT_PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(int(pid)))
    except Exception:
        pass

def _remove_pidfile():
    try:
        if os.path.isfile(BOT_PIDFILE):
            os.remove(BOT_PIDFILE)
    except Exception:
        pass

def _is_pid_running(pid):
    try:
        if pid <= 0:
            return False
    except Exception:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True

def _find_running_bot_by_psutil():
    if not psutil:
        return False
    try:
        target_basename = os.path.basename(BOT_SCRIPT)
        for p in psutil.process_iter(attrs=["pid", "cmdline"]):
            try:
                info = p.info
                pid = info.get("pid")
                if pid == os.getpid():
                    continue
                cmdline = info.get("cmdline") or []
                for part in cmdline:
                    if not part:
                        continue
                    pn = os.path.basename(part).upper()
                    if pn == target_basename.upper():
                        return True
                    if os.path.abspath(part) == os.path.abspath(BOT_SCRIPT):
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False

def is_bot_running():
    try:
        if _find_running_bot_by_psutil():
            return True
    except Exception:
        pass
    pid = _read_pidfile()
    if pid is None:
        return False
    if _is_pid_running(pid):
        return True
    _remove_pidfile()
    return False

def _cleanup_launched_processes(timeout=5):
    with launched_lock:
        procs = list(launched_procs)
        launched_procs.clear()
    for proc in procs:
        try:
            if proc.poll() is None:
                print(f"Terminating bot pid={proc.pid} ...")
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=timeout)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except Exception:
            pass
    _remove_pidfile()

def launch_bot_async(args=None):
    if not BOT_SCRIPT:
        return
    if not os.path.isfile(BOT_SCRIPT):
        print(f"Bot script not found at '{BOT_SCRIPT}'; skipping bot call.")
        return
    with bot_launch_lock:
        if is_bot_running():
            print("Bot already running; skipping launch.")
            return
        def _run():
            cmd = [sys.executable, BOT_SCRIPT]
            if args:
                cmd.extend(args)
            # Forward TBOT_* env vars so Tbotx.py gets per-user file paths
            env = os.environ.copy()
            try:
                proc = subprocess.Popen(cmd, env=env)
                with launched_lock:
                    launched_procs.append(proc)
                _write_pidfile(proc.pid)
                print(f"Launched bot: {' '.join(cmd)} (pid {proc.pid})")
            except Exception as e:
                print("Failed to launch bot:", e)
        t = threading.Thread(target=_run, daemon=True)
        t.start()

# -------------------------
# Config writer (previous simple format, prefer continuation signals)
# -------------------------
def write_top5_to_config():
    """
    Writes config.json with the previous requested format:
      "pairs": [ { "symbol": "...", "trend": "..." }, ... ]
    Selection:
      - Filter collected_trends by _allowed_for_output
      - Sort so that entries with a continuation signal are preferred (signal present first),
        then by strength descending.
      - For each chosen pair, use its continuation signal as the "trend" value if present;
        otherwise use the computed trend.
    """
    with collected_lock:
        filtered = [
            e for e in collected_trends
            if _allowed_for_output(e.get("symbol_norm", "").upper(), (e.get("market") or "").lower())
        ]
        # sort by (has_signal, not_exhausted, strength), all descending
        def sort_key(e):
            has_signal    = 1 if e.get("signal") else 0
            not_exhausted = 0 if e.get("rsi_exhausted") else 1
            return (has_signal, not_exhausted, e.get("strength", 0))
        entries = sorted(filtered, key=sort_key, reverse=True)
        top5 = entries[:5]

    pairs = []
    for e in top5:
        trend_value = e.get("signal") if e.get("signal") is not None else e.get("trend")
        pairs.append({"symbol": e.get("symbol", ""), "trend": trend_value})

    # pad to 5
    while len(pairs) < 5:
        pairs.append({"symbol": "", "trend": None})

    cfg = {"pairs": pairs}

    try:
        # Load existing per-user config — preserves app_id, api_token and any other keys
        config_path = _CONFIG_PATH
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                # Only update pairs — never touch app_id or api_token
                existing["pairs"] = pairs
                cfg = existing
            except Exception:
                pass  # if existing file is corrupt, write fresh but keep pairs

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        print("Wrote top 5 trending pairs to config.json (signals preferred as trend where present)")
        if CALL_BOT_ON_UPDATE:
            launch_bot_async(args=["--config", config_path])
    except Exception as exc:
        print("Failed to write config.json:", exc)

# -------------------------
# Background threads
# -------------------------
def periodic_active_symbols_sender(ws):
    while not stop_event.wait(UPDATE_INTERVAL):
        try:
            req = {"active_symbols": "brief", "product_type": "basic"}
            ws.send(json.dumps(req))
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sent periodic active_symbols request")
        except Exception as e:
            print("Error sending periodic active_symbols request:", e)

def ping_loop(ws):
    while not stop_event.wait(PING_INTERVAL):
        try:
            if getattr(ws, "sock", None) and getattr(ws.sock, "connected", False):
                try:
                    ws.send("ping", opcode=websocket.ABNF.OPCODE_PING)
                except Exception:
                    try:
                        ws.send_ping()
                    except Exception:
                        pass
        except Exception:
            pass

# -------------------------
# WebSocket handlers
# -------------------------
def on_open(ws):
    print("Connected to Deriv API")
    req = {"active_symbols": "brief", "product_type": "basic"}
    ws.send(json.dumps(req))
    t = threading.Thread(target=periodic_active_symbols_sender, args=(ws,), daemon=True)
    t.start()
    p = threading.Thread(target=ping_loop, args=(ws,), daemon=True)
    p.start()

def on_message(ws, message):
    global total_requests, current_cycle
    try:
        data = json.loads(message)
    except Exception:
        return

    # active_symbols response (start a new cycle)
    if "active_symbols" in data:
        symbols = data["active_symbols"]
        target_symbols = [s for s in symbols if is_target_symbol(s)]
        if SYMBOL_LIMIT is not None:
            target_symbols = target_symbols[:SYMBOL_LIMIT]

        with cycle_lock:
            current_cycle += 1
            cycle_id = current_cycle

        with collected_lock:
            collected_trends.clear()

        print(f"\nCycle {cycle_id}: Found {len(target_symbols)} matching symbols (requesting {CANDLE_COUNT} 1-min candles each)...\n")

        with total_requests_lock:
            total_requests = len(target_symbols)

        for symbol in target_symbols:
            symbol_name_raw = (symbol.get("symbol") or "").strip()
            symbol_name_norm = symbol_name_raw.upper()
            market_name = (symbol.get("market_display_name") or symbol.get("market") or "").strip()
            rid = next_req_id()
            pending_requests[rid] = {
                "symbol": symbol_name_raw,
                "symbol_norm": symbol_name_norm,
                "market": market_name,
                "cycle": cycle_id
            }
            req = {
                "ticks_history": symbol_name_raw,
                "style": "candles",
                "granularity": GRANULARITY,
                "count": CANDLE_COUNT,
                "end": "latest",
                "req_id": rid
            }
            ws.send(json.dumps(req))
            time.sleep(0.05)
        return

    # candle responses
    candles = try_extract_candles(data)
    if candles is not None:
        req_id = data.get("req_id")
        if req_id is None:
            req_id = data.get("echo_req", {}).get("req_id")
        req_id = normalize_req_id(req_id)

        meta = None
        if req_id is not None:
            meta = pending_requests.pop(req_id, None)

        if meta is None:
            echo = data.get("echo_req", {})
            symbol_from_echo = (echo.get("ticks_history") or echo.get("symbol") or "").strip()
            if symbol_from_echo:
                with cycle_lock:
                    cycle_id = current_cycle
                meta = pop_meta_by_symbol(symbol_from_echo, cycle_id)

        if meta is None:
            symbol_echo = (data.get("echo_req", {}).get("ticks_history") or "unknown")
            meta = {
                "symbol": symbol_echo,
                "symbol_norm": symbol_echo.upper(),
                "market": (data.get("echo_req", {}).get("market") or ""),
                "cycle": None
            }

        with cycle_lock:
            cycle_id = current_cycle
        if meta.get("cycle") is not None and meta.get("cycle") != cycle_id:
            print(f"Ignoring response for {meta.get('symbol')} from old cycle {meta.get('cycle')}")
            return

        closes = []
        highs = []
        lows = []
        for c in candles:
            try:
                if "close" in c:
                    closes.append(float(c["close"]))
                else:
                    closes.append(None)
                if "high" in c:
                    highs.append(float(c["high"]))
                else:
                    highs.append(None)
                if "low" in c:
                    lows.append(float(c["low"]))
                else:
                    lows.append(None)
            except Exception:
                continue

        if not closes or any(v is None for v in closes):
            print(f"No valid closes for {meta.get('symbol')} (req_id={req_id})")
            with total_requests_lock:
                total_requests -= 1
                if total_requests == 0:
                    write_top5_to_config()
            return

        if any(v is None for v in highs) or any(v is None for v in lows):
            highs = None
            lows = None

        ema20 = calculate_ema(closes, EMA_PERIOD)

        if len(closes) < (EMA_PERIOD + 1):
            print(f"{meta['symbol']} | {meta['market']} | Not enough data for EMA({EMA_PERIOD}) comparison with previous closed candle (need {EMA_PERIOD + 1}, got {len(closes)})")
            with total_requests_lock:
                total_requests -= 1
                if total_requests == 0:
                    write_top5_to_config()
            return

        prev_idx = -2
        prev_close = closes[prev_idx]
        prev_ema = ema20[prev_idx]

        if prev_close > prev_ema:
            trend = "up"
        elif prev_close < prev_ema:
            trend = "down"
        else:
            trend = "sideways"

        # strength calculation (prefer exhaustion)
        strength = 0.0
        used_exhaustion = False
        atr14 = None
        if highs is not None and lows is not None:
            atr14 = calculate_atr(highs, lows, closes, ATR_PERIOD)
            if len(atr14) == len(closes):
                atr_prev = atr14[prev_idx]
                if atr_prev is not None and atr_prev != 0:
                    dist_prev = prev_close - prev_ema
                    strength = abs(dist_prev / atr_prev)
                    used_exhaustion = True
        if not used_exhaustion:
            if prev_ema and prev_ema != 0:
                strength = abs((prev_close - prev_ema) / prev_ema)
            else:
                strength = 0.0

        # continuation signal detection
        signal = None
        last_close = closes[-1]
        last_ema = ema20[-1] if ema20 else None
        if last_ema is not None and len(closes) >= 3:
            atr_last = None
            if atr14 and len(atr14) == len(closes):
                atr_last = atr14[-1]
            if atr_last is not None and atr_last != 0:
                threshold = abs(atr_last) * 0.5
            else:
                threshold = max(abs(last_ema) * 0.001, 1e-8)
            close_to_ema = abs(last_close - last_ema) <= threshold
            prev_prev_close = closes[-3]
            bullish_momentum = (prev_prev_close < prev_close)
            bearish_momentum = (prev_prev_close > prev_close)
            if close_to_ema and bullish_momentum and prev_close > prev_ema:
                signal = "buy"
            elif close_to_ema and bearish_momentum and prev_close < prev_ema:
                signal = "sell"

        # ── RSI trend exhaustion filter ──────────────────────────────────────────
        rsi_values = calculate_rsi(closes, RSI_PERIOD)
        rsi_prev   = rsi_values[prev_idx] if len(rsi_values) >= abs(prev_idx) else None
        rsi_last   = rsi_values[-1]       if rsi_values else None

        rsi_exhausted = False
        rsi_exhaustion_reason = ""

        if rsi_prev is not None:
            if trend == "up" and rsi_prev >= RSI_OVERBOUGHT:
                rsi_exhausted = True
                rsi_exhaustion_reason = f"uptrend exhausted (RSI={rsi_prev:.1f} >= {RSI_OVERBOUGHT})"
            elif trend == "down" and rsi_prev <= RSI_OVERSOLD:
                rsi_exhausted = True
                rsi_exhaustion_reason = f"downtrend exhausted (RSI={rsi_prev:.1f} <= {RSI_OVERSOLD})"

        # Suppress continuation signals when RSI is at extreme levels
        if signal == "buy"  and rsi_last is not None and rsi_last >= RSI_EXTREME_OB:
            signal = None
            rsi_exhaustion_reason += f" | buy signal suppressed (RSI={rsi_last:.1f} >= {RSI_EXTREME_OB})"
        elif signal == "sell" and rsi_last is not None and rsi_last <= RSI_EXTREME_OS:
            signal = None
            rsi_exhaustion_reason += f" | sell signal suppressed (RSI={rsi_last:.1f} <= {RSI_EXTREME_OS})"

        if rsi_exhaustion_reason:
            print(f"  RSI Filter → {rsi_exhaustion_reason}")
        # ─────────────────────────────────────────────────────────────────────────

        with collected_lock:
            collected_trends.append({
                "symbol": meta["symbol"],
                "symbol_norm": meta.get("symbol_norm", (meta.get("symbol") or "").upper()),
                "market": meta.get("market", ""),
                "trend": trend,
                "strength": strength,
                "signal": signal,
                "rsi": rsi_prev,
                "rsi_exhausted": rsi_exhausted,
            })

        rsi_tag = f" | RSI({RSI_PERIOD}): {rsi_prev:.1f}{'⚠' if rsi_exhausted else ''}" if rsi_prev is not None else ""
        if last_ema is not None:
            print(f"{meta['symbol']} | {meta['market']} | Latest Close: {last_close:.5f} | Prev Close: {prev_close:.5f} | Prev EMA({EMA_PERIOD}): {prev_ema:.5f} | Trend (by prev closed candle): {trend} | Strength: {strength:.4f} | Signal: {signal}{rsi_tag}")
        else:
            print(f"{meta['symbol']} | {meta['market']} | Latest Close: {last_close:.5f} | Prev Close: {prev_close:.5f} | Prev EMA({EMA_PERIOD}): n/a | Trend: {trend} | Strength: {strength:.4f} | Signal: {signal}{rsi_tag}")

        if highs is not None and lows is not None and atr14 and len(atr14) == len(closes):
            atr_prev = atr14[prev_idx]
            if atr_prev is not None and atr_prev != 0:
                dist_prev = prev_close - prev_ema
                score = dist_prev / atr_prev
                percent = min(abs(score) / 2.0 * 100, 100)
                direction = "Bullish Stretch" if score > 0 else "Bearish Stretch"
                print("\nExhaustion (prev candle):")
                print(f"{percent:.1f}% Exhausted ({direction})\n")

        if CALL_BOT_ON_EACH_SYMBOL:
            launch_bot_async(args=["--symbol", meta["symbol"], "--trend", trend, "--signal", str(signal)])

        with total_requests_lock:
            total_requests -= 1
            if total_requests == 0:
                write_top5_to_config()
        return

    if "error" in data:
        print("API Error:", data["error"])

def on_error(ws, error):
    print("Error:", error)

def on_close(ws, close_status_code, close_msg):
    print("Connection closed")
    stop_event.set()

# -------------------------
# Shutdown handling
# -------------------------
def _shutdown_and_exit(signum=None, frame=None):
    print("Shutdown requested (signal/interrupt). Cleaning up ...")
    stop_event.set()
    time.sleep(0.1)
    _cleanup_launched_processes(timeout=3)
    try:
        sys.exit(0)
    except SystemExit:
        os._exit(0)

signal.signal(signal.SIGINT, _shutdown_and_exit)
try:
    signal.signal(signal.SIGTERM, _shutdown_and_exit)
except Exception:
    pass

# -------------------------
# Main
# -------------------------
if __name__ == "__main__":
    _config_path = _CONFIG_PATH
    try:
        open(_config_path, "a").close()
    except Exception:
        print(f"Warning: cannot create {_config_path}. Check permissions.")

    # cleanup stale pidfile
    try:
        pid = _read_pidfile()
        if pid is not None and not _is_pid_running(pid):
            _remove_pidfile()
    except Exception:
        pass

    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    try:
        ws.run_forever(ping_interval=PING_INTERVAL, ping_timeout=5)
    except KeyboardInterrupt:
        _shutdown_and_exit()
    except Exception as e:
        print("WebSocket run_forever ended with exception:", e)
        _shutdown_and_exit()
