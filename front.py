import os
import sys
import pty
import json
import sqlite3
import select
import time
import errno
import threading
import queue
import websocket as _ws_client
import signal as _signal

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "CHANGE-THIS-TO-A-LONG-RANDOM-STRING"

try:
    import gevent.monkey
    gevent.monkey.patch_all()
    _async_mode = "gevent"
except ImportError:
    _async_mode = "threading"

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode=_async_mode,
    logger=False,
    engineio_logger=False,
)

# TBOT_DATA_DIR env var lets you redirect all persistent files to a mounted disk
# (e.g. Render Disk at /data). Falls back to the script directory if not set.
BASE_DIR = os.environ.get("TBOT_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CONFIG = {
    "app_id":    "",
    "api_token": "",
    "pairs": [
        {"symbol": "R_100", "trend": None},
        {"symbol": "R_50",  "trend": None},
        {"symbol": "R_25",  "trend": None},
        {"symbol": "R_10",  "trend": None},
        {"symbol": "R_5",   "trend": None},
    ]
}

# ── PER-SESSION STATE ─────────────────────────────────────────────────────────
#
#  All dicts keyed by socket SID (unique per browser connection).
#  Cleaned up automatically on disconnect — zero cross-user leakage.
#
#  sessions[sid]  = PTY file descriptor
#  pids[sid]      = child process PID
#  buffers[sid]   = rolling terminal output (string)
#  configs[sid]   = user's verified Deriv config (never written to shared disk)
#  names[sid]     = license name for this connection

sessions = {}
pids     = {}
buffers  = {}
configs  = {}
names    = {}


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _safe_name(name):
    """Sanitise license name so it is safe to use as part of a filename."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


# ── LICENSE ───────────────────────────────────────────────────────────────────

def validate_license(name, token):
    db_path = os.path.join(BASE_DIR, "license.db")
    conn = sqlite3.connect(db_path)
    c    = conn.cursor()
    rows = c.execute("SELECT data FROM licenses").fetchall()
    conn.close()

    for (raw,) in rows:
        data = json.loads(raw)
        if data["name"] == name and data["license_token"] == token:
            if time.time() > data["expiry"]:
                return False, "LICENSE EXPIRED"
            return True, "VALID"

    return False, "INVALID LICENSE"


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("terminal.html")


@app.route("/verify_and_save", methods=["POST"])
def verify_and_save():
    """
    Verify Deriv credentials server-side.
    Config stored in memory under this socket's SID — no shared file written.
    Browser sends its socket ID in the X-Socket-ID header.
    """
    try:
        sid       = request.headers.get("X-Socket-ID", "")
        body      = request.get_json(silent=True) or {}
        app_id    = str(body.get("app_id",    "")).strip()
        api_token = str(body.get("api_token", "")).strip()

        if not app_id:
            return jsonify({"ok": False, "error": "App ID is required"}), 400
        if not api_token:
            return jsonify({"ok": False, "error": "API Token is required"}), 400

        result_q = queue.Queue()

        def _on_open(ws):
            ws.send(json.dumps({"authorize": api_token}))

        def _on_message(ws, message):
            ws.close()
            result_q.put(("ok", message))

        def _on_error(ws, error):
            result_q.put(("error", str(error)))

        def _on_close(ws, *a):
            if result_q.empty():
                result_q.put(("error", "Connection closed without response"))

        ws_url = f"wss://ws.derivws.com/websockets/v3?app_id={app_id}"
        ws = _ws_client.WebSocketApp(
            ws_url,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )

        t = threading.Thread(target=ws.run_forever, kwargs={"ping_timeout": 10}, daemon=True)
        t.start()
        t.join(timeout=15)

        if result_q.empty():
            return jsonify({"ok": False, "error": "Deriv connection timed out"}), 408

        status, payload = result_q.get()
        if status == "error":
            return jsonify({"ok": False, "error": f"Deriv error: {payload}"}), 400

        resp = json.loads(payload)
        if "error" in resp:
            code_map = {
                "InvalidToken":     "Invalid API token.",
                "InvalidAppID":     "Invalid App ID.",
                "RateLimit":        "Rate limited — wait a moment.",
                "DisabledClient":   "Account disabled.",
                "AccountSuspended": "Account suspended.",
            }
            err_code = resp["error"].get("code", "")
            err_msg  = code_map.get(err_code, resp["error"].get("message", "Authorization failed"))
            return jsonify({"ok": False, "error": err_msg}), 401

        auth    = resp.get("authorize", {})
        account = auth.get("fullname") or auth.get("email") or auth.get("loginid") or ""

        # Store config in memory for this SID — never touches a shared config.json
        if sid:
            cfg = dict(DEFAULT_CONFIG)
            cfg["app_id"]    = app_id
            cfg["api_token"] = api_token
            configs[sid]     = cfg

        print(f"[verify_and_save] OK — account={account!r}  app_id={app_id!r}  sid={sid!r}")
        return jsonify({"ok": True, "account": account})

    except Exception as e:
        print(f"[verify_and_save] ERROR: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/save_config", methods=["POST"])
def save_config():
    """Store trading config in memory for this SID. Nothing written to shared disk."""
    try:
        sid  = request.headers.get("X-Socket-ID", "")
        body = request.get_json(silent=True)

        if not body:
            return jsonify({"ok": False, "error": "No JSON body received"}), 400

        app_id    = str(body.get("app_id",    "")).strip()
        api_token = str(body.get("api_token", "")).strip()

        if not app_id:
            return jsonify({"ok": False, "error": "app_id is required"}), 400
        if not api_token:
            return jsonify({"ok": False, "error": "api_token is required"}), 400

        cfg = configs.get(sid, dict(DEFAULT_CONFIG))
        cfg["app_id"]    = app_id
        cfg["api_token"] = api_token
        configs[sid]     = cfg

        return jsonify({"ok": True})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/get_config")
def get_config():
    """Return this user's in-memory config (empty defaults if not set yet)."""
    sid = request.headers.get("X-Socket-ID", "")
    cfg = configs.get(sid, dict(DEFAULT_CONFIG))
    return jsonify(cfg)


# ── SOCKET EVENTS ─────────────────────────────────────────────────────────────

@socketio.on("check_license")
def check_license(data):
    name  = data.get("name", "")
    token = data.get("token", "")
    valid, msg = validate_license(name, token)
    emit("license_result", {"valid": valid, "msg": msg})


@socketio.on("start_session")
def start_session(data):
    sid   = request.sid          # unique per browser connection
    name  = data["name"]
    token = data["token"]

    valid, msg = validate_license(name, token)
    if not valid:
        emit("terminal_output", f"\r\n{msg}\r\n")
        return

    # Clean up any stale session this socket had before reconnecting
    if sid in sessions:
        _cleanup_session(sid)

    names[sid] = name

    # Write a temp per-session config file for the bot process to read.
    # Named by SID (short-lived) — credentials not persisted long term.
    user_config_path = os.path.join(BASE_DIR, f"config_{sid}.json")
    user_cfg = configs.get(sid, dict(DEFAULT_CONFIG))
    with open(user_config_path, "w") as f:
        json.dump(user_cfg, f, indent=4)

    pid, fd = pty.fork()

    if pid == 0:
        # ── Child process ──
        try:
            os.setpgrp()
        except OSError:
            pass

        script = os.path.join(BASE_DIR, "Tbot_trend.py")   # trend scanner launches 5min_tbot.py
        os.chdir(BASE_DIR)
        python = sys.executable or "python3"

        # Pass all per-user context via environment variables.
        # TBOT_USER  = stable license name  → persistent files (win/loss setups, phase)
        # TBOT_SID   = this connection's ID → temp session files
        # TBOT_CONFIG / TBOT_PID_FILE       → exact file paths for this session
        os.environ["TBOT_USER"]     = _safe_name(name)
        os.environ["TBOT_SID"]      = sid
        os.environ["TBOT_CONFIG"]   = user_config_path
        os.environ["TBOT_PID_FILE"] = os.path.join(BASE_DIR, f"bot_{sid}.pid")

        try:
            os.execvp(python, [python, script])
        except Exception as e:
            os.write(1, f"\r\n[EXEC ERROR] {e}\r\n".encode())
            os._exit(1)
    else:
        # ── Parent: track fd and pid for this SID ──
        sessions[sid] = fd
        pids[sid]     = pid
        buffers[sid]  = ""
        socketio.start_background_task(read_terminal, sid)


def read_terminal(sid):
    """Read PTY output and emit ONLY to the socket that owns this session."""
    while True:
        time.sleep(0.02)

        if sid not in sessions:
            break

        fd = sessions[sid]

        try:
            r, _, _ = select.select([fd], [], [], 0)
        except (ValueError, OSError):
            _cleanup_session(sid)
            break

        if not r:
            continue

        try:
            data = os.read(fd, 1024).decode(errors="ignore")
        except OSError as e:
            msg = "\r\n[process exited]\r\n" if e.errno in (errno.EIO, errno.EBADF) \
                  else f"\r\n[read error: {e}]\r\n"
            socketio.emit("terminal_output", msg, to=sid)
            _cleanup_session(sid)
            break
        except Exception as e:
            socketio.emit("terminal_output", f"\r\n[unexpected error: {e}]\r\n", to=sid)
            _cleanup_session(sid)
            break

        if not data:
            socketio.emit("terminal_output", "\r\n[process exited]\r\n", to=sid)
            _cleanup_session(sid)
            break

        buffers[sid] = (buffers.get(sid, "") + data)[-20000:]
        socketio.emit("terminal_output", data, to=sid)   # ← private to this user only


def _cleanup_session(sid):
    """Kill the PTY process and remove all in-memory state for this SID.
    Persistent user files (win/loss setups, phase state) are NOT deleted —
    they survive and are reloaded next time the same license name connects."""

    fd  = sessions.pop(sid, None)
    pid = pids.pop(sid, None)
    buffers.pop(sid, None)
    configs.pop(sid, None)
    names.pop(sid, None)

    # Delete temp session config — credentials should not linger on disk
    user_config_path = os.path.join(BASE_DIR, f"config_{sid}.json")
    if os.path.exists(user_config_path):
        try:
            os.remove(user_config_path)
        except OSError:
            pass

    if pid is not None:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, _signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, _signal.SIGTERM)
            except OSError:
                pass

        time.sleep(1)

        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, _signal.SIGKILL)
        except OSError:
            pass

    # Clean up per-session pid file
    bot_pidfile = os.path.join(BASE_DIR, f"bot_{sid}.pid")
    if os.path.exists(bot_pidfile):
        try:
            with open(bot_pidfile) as f:
                bot_pid = int(f.read().strip())
            try:
                os.killpg(os.getpgid(bot_pid), _signal.SIGTERM)
            except OSError:
                try:
                    os.kill(bot_pid, _signal.SIGTERM)
                except OSError:
                    pass
        except Exception:
            pass
        try:
            os.remove(bot_pidfile)
        except OSError:
            pass

    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


@socketio.on("terminal_input")
def terminal_input(data):
    sid = request.sid     # always use SID — never trust data["name"] for routing
    key = data["key"]

    if sid in sessions:
        try:
            os.write(sessions[sid], key.encode())
        except OSError:
            pass


@socketio.on("stop_session")
def stop_session(data):
    sid = request.sid
    _cleanup_session(sid)
    emit("terminal_output", "\r\nSESSION STOPPED\r\n")


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    if sid in sessions:
        _cleanup_session(sid)


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket as _socket
    try:
        local_ip = _socket.gethostbyname(_socket.gethostname())
    except Exception:
        local_ip = "localhost"
    print(f"\n  T-BOT Server")
    print(f"  → http://localhost:5000")
    print(f"  → http://{local_ip}:5000\n")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, use_reloader=False)
