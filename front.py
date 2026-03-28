import os, sys, json, sqlite3, time, errno, subprocess
import threading, queue, secrets, string, smtplib
import logging
import websocket as _ws_client
import signal as _signal
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, jsonify,
                   session as flask_session, redirect, Response)
from flask_socketio import SocketIO, emit

# ── Logging — writes to stdout which Render captures reliably ────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('tbot')
# Also ensure stdout is unbuffered
sys.stdout.reconfigure(line_buffering=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-in-production")

# Using threading mode — works on all Python versions including 3.14
# gevent removed because it requires C compilation and fails on Python 3.14
_async_mode = "threading"

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=True,
    engineio_logger=True,
    allow_upgrades=True,
    ping_timeout=60,
    ping_interval=25,
)

BASE_DIR       = os.environ.get("TBOT_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
GMAIL_ADDRESS  = os.environ.get("GMAIL_ADDRESS",  "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")
FROM_NAME      = os.environ.get("FROM_NAME",      "TBOT")
APP_URL        = os.environ.get("APP_URL",        "https://tbot-1m2b.onrender.com")
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL",    GMAIL_ADDRESS)
USDT_ADDRESS   = os.environ.get("USDT_ADDRESS",  "")
PRICE_USD      = os.environ.get("PRICE_USD",      "20")
LICENSE_DAYS   = int(os.environ.get("LICENSE_DAYS", "30"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


DEFAULT_CONFIG = {"app_id":"","api_token":"","pairs":[
    {"symbol":"R_100","trend":None},{"symbol":"R_50","trend":None},
    {"symbol":"R_25","trend":None},{"symbol":"R_10","trend":None},
    {"symbol":"R_5","trend":None}]}

sessions={}; procs={}; buffers={}; configs={}; names={}
active_users={}   # name -> sid — current connected socket
bot_sessions={}   # name -> {proc, pipe, config} — bot processes keyed by NAME
                  # bot keeps running even when browser disconnects
BUFFER_SIZE=50000 # bytes of terminal output kept for reconnect replay
IDLE_TIMEOUT=3600 # seconds — kill bot if user disconnected this long (1 hour)

# ── EMAIL STATUS STORE ────────────────────────────────────────────────────────
# Keyed by a short id; background email threads write here;
# browser polls /email-status/<key> to show JS alerts.
_email_status      = {}
_email_status_lock = threading.Lock()

def _es_set(key, status, msg=""):
    with _email_status_lock:
        _email_status[key] = {"status": status, "msg": msg}

def _es_get(key):
    with _email_status_lock:
        return _email_status.get(key, {"status": "pending", "msg": ""})

def _safe_name(n):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in n)

# ── DB ────────────────────────────────────────────────────────────────────────
def _db():
    return sqlite3.connect(os.path.join(BASE_DIR,"license.db"))

def _db_init():
    c=_db()
    c.execute("CREATE TABLE IF NOT EXISTS licenses(id INTEGER PRIMARY KEY AUTOINCREMENT,data TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS pending_payments(id INTEGER PRIMARY KEY AUTOINCREMENT,"
              "name TEXT,email TEXT,proof_notes TEXT,submitted_at REAL,status TEXT DEFAULT 'pending')")
    c.commit(); c.close()

_db_init()

# ── TOKEN ─────────────────────────────────────────────────────────────────────
def _gen_token():
    a=string.ascii_uppercase+string.digits
    r="".join(secrets.choice(a) for _ in range(32))
    return "-".join(r[i:i+4] for i in range(0,32,4))

# ── LICENSE HELPERS ───────────────────────────────────────────────────────────
def _load_licenses():
    c=_db(); rows=c.execute("SELECT id,data FROM licenses").fetchall(); c.close()
    out=[]
    for rid,raw in rows:
        try: d=json.loads(raw); d["_id"]=rid; out.append(d)
        except: pass
    return out

def _pending_count():
    try:
        c=_db(); n=c.execute("SELECT COUNT(*) FROM pending_payments WHERE status='pending'").fetchone()[0]; c.close(); return n
    except: return 0

def _create_license(name,email,days):
    token=_gen_token(); exp=time.time()+days*86400
    rec={"name":name,"email":email,"license_token":token,"expiry":exp,
         "created_at":datetime.now().strftime("%Y-%m-%d %H:%M")}
    c=_db(); c.execute("INSERT INTO licenses(data) VALUES(?)",(json.dumps(rec),))
    c.commit(); c.close(); return rec

def validate_license(name,token):
    c=_db(); rows=c.execute("SELECT data FROM licenses").fetchall(); c.close()
    for (raw,) in rows:
        d=json.loads(raw)
        if d["name"]==name and d["license_token"]==token:
            return (False,"LICENSE EXPIRED") if time.time()>d["expiry"] else (True,"VALID")
    return False,"INVALID LICENSE"

# ── EMAIL ─────────────────────────────────────────────────────────────────────
def _send_email(to, subject, html, text):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASS:
        log.error("[email] ERROR: GMAIL_ADDRESS or GMAIL_APP_PASS not set")
        raise ValueError("GMAIL_ADDRESS and GMAIL_APP_PASS not configured.")

    log.info(f"[email] Sending → to={to!r} from={GMAIL_ADDRESS!r}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{GMAIL_ADDRESS}>"
    msg["To"]      = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        server.sendmail(GMAIL_ADDRESS, to, msg.as_string())

    log.info(f"[email] ✓ Sent to {to}")

def _email_license(name,email,token,expiry_ts,days):
    exp=datetime.fromtimestamp(expiry_ts).strftime("%Y-%m-%d")
    html=(f'<!DOCTYPE html><html><body style="background:#090c10;font-family:Courier New,monospace;padding:40px">'
          f'<div style="max-width:560px;margin:0 auto;background:#0e1318;border:1px solid #1a2130;border-radius:12px;overflow:hidden">'
          f'<div style="background:#0b1017;border-bottom:2px solid #00e5a0;padding:24px 32px">'
          f'<span style="font-size:22px;font-weight:900;color:#fff">T<span style="color:#00e5a0">BOT</span></span>'
          f'<span style="font-size:10px;color:#4a6070;letter-spacing:3px;margin-left:12px;text-transform:uppercase">KEY DELIVERY</span></div>'
          f'<div style="padding:32px">'
          f'<p style="color:#c8d8e8;font-size:15px;margin:0 0 20px">Hi <strong style="color:#fff">{name}</strong>,</p>'
          f'<p style="color:#4a6070;font-size:13px;margin:0 0 24px">Payment confirmed. Your license is active.</p>'
          f'<div style="background:rgba(0,229,160,.04);border:1px solid rgba(0,229,160,.2);border-radius:8px;padding:20px;margin-bottom:20px">'
          f'<p style="font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#4a6070;margin:0 0 8px">License Token</p>'
          f'<p style="font-size:14px;color:#00e5a0;letter-spacing:1px;word-break:break-all;margin:0;font-weight:600">{token}</p></div>'
          f'<table style="width:100%;border:1px solid #1a2130;border-radius:8px;border-collapse:collapse;margin-bottom:24px">'
          f'<tr><td style="padding:10px 16px;font-size:10px;color:#4a6070;border-bottom:1px solid #1a2130;width:35%;text-transform:uppercase;letter-spacing:1px">Username</td>'
          f'<td style="padding:10px 16px;font-size:13px;color:#fff;border-bottom:1px solid #1a2130">{name}</td></tr>'
          f'<tr><td style="padding:10px 16px;font-size:10px;color:#4a6070;border-bottom:1px solid #1a2130;text-transform:uppercase;letter-spacing:1px">Valid For</td>'
          f'<td style="padding:10px 16px;font-size:13px;color:#fff;border-bottom:1px solid #1a2130">{days} days</td></tr>'
          f'<tr><td style="padding:10px 16px;font-size:10px;color:#4a6070;text-transform:uppercase;letter-spacing:1px">Expires</td>'
          f'<td style="padding:10px 16px;font-size:13px;color:#fff">{exp}</td></tr></table>'
          f'<div style="text-align:center"><a href="{APP_URL}" style="background:#00e5a0;color:#000;font-weight:900;'
          f'font-size:13px;letter-spacing:2px;text-transform:uppercase;text-decoration:none;padding:14px 36px;border-radius:8px;display:inline-block">'
          f'Access Terminal &rarr;</a></div></div>'
          f'<div style="background:#0b1017;padding:14px 32px;text-align:center">'
          f'<p style="font-size:10px;color:#4a6070;margin:0">Keep this key private. Contact support if compromised.</p></div>'
          f'</div></body></html>')
    text=(f"Hi {name},\n\nYour license is ready.\n\n  Username: {name}\n  Token   : {token}\n"
          f"  Valid for: {days} days\n  Expires : {exp}\n\nLogin: {APP_URL}\n\n-- {FROM_NAME}")
    _send_email(email,f"Your TBOT License Key — {name}",html,text)

def _email_admin_notify(name,email,proof,pid):
    url=f"{APP_URL}/admin/payments"
    html=(f'<div style="font-family:sans-serif;background:#090c10;padding:40px;color:#c9d1d9">'
          f'<div style="max-width:500px;margin:0 auto;background:#0e1318;border:1px solid #1a2130;border-radius:12px;overflow:hidden">'
          f'<div style="background:#0b1017;border-bottom:2px solid #f59e0b;padding:20px 28px">'
          f'<span style="font-size:18px;font-weight:900;color:#fff">T<span style="color:#00e5a0">BOT</span></span>'
          f'<span style="margin-left:12px;font-size:11px;color:#f59e0b;letter-spacing:2px">PAYMENT PENDING</span></div>'
          f'<div style="padding:24px 28px">'
          f'<p style="font-size:14px;margin:0 0 16px">New payment submitted — action required.</p>'
          f'<table style="width:100%;border:1px solid #1a2130;border-radius:8px;border-collapse:collapse">'
          f'<tr><td style="padding:9px 14px;font-size:10px;color:#4a6070;border-bottom:1px solid #1a2130;width:30%;text-transform:uppercase;letter-spacing:1px">Name</td>'
          f'<td style="padding:9px 14px;font-size:13px;color:#fff;border-bottom:1px solid #1a2130">{name}</td></tr>'
          f'<tr><td style="padding:9px 14px;font-size:10px;color:#4a6070;border-bottom:1px solid #1a2130;text-transform:uppercase;letter-spacing:1px">Email</td>'
          f'<td style="padding:9px 14px;font-size:13px;color:#fff;border-bottom:1px solid #1a2130">{email}</td></tr>'
          f'<tr><td style="padding:9px 14px;font-size:10px;color:#4a6070;text-transform:uppercase;letter-spacing:1px">TX / Notes</td>'
          f'<td style="padding:9px 14px;font-size:13px;color:#fff">{proof or "(none)"}</td></tr></table>'
          f'<div style="text-align:center;margin-top:20px">'
          f'<a href="{url}" style="background:#00e5a0;color:#000;font-weight:900;font-size:13px;'
          f'letter-spacing:1px;text-transform:uppercase;text-decoration:none;padding:12px 24px;border-radius:8px;display:inline-block">'
          f'Review Payment &rarr;</a></div></div></div></div>')
    text=f"New payment proof submitted.\n\n  Name : {name}\n  Email: {email}\n  Notes: {proof or '(none)'}\n\nReview: {url}"
    _send_email(ADMIN_EMAIL,f"[TBOT] Payment Proof — {name} ({email})",html,text)

# ── ADMIN HELPERS ─────────────────────────────────────────────────────────────
def _admin_required(f):
    @wraps(f)
    def d(*a,**kw):
        if not ADMIN_PASSWORD: return Response("Set ADMIN_PASSWORD env var.",403)
        if not flask_session.get("admin_logged_in"): return redirect("/x7k2/login")
        return f(*a,**kw)
    return d

_CSS="""<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;
min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:28px 14px}
h1{color:#58a6ff;font-size:20px;margin-bottom:3px}.sub{color:#8b949e;font-size:11px;margin-bottom:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:22px 26px;
width:100%;max-width:980px;margin-bottom:18px}
.card h3{color:#58a6ff;font-size:10px;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:16px}
input,textarea{background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;
padding:8px 11px;font-size:12px;width:100%;outline:none;transition:border-color .2s;font-family:inherit}
input:focus,textarea:focus{border-color:#58a6ff}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
label{display:block;font-size:10px;color:#8b949e;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:4px}
.btn{padding:7px 14px;border:none;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;
transition:opacity .15s;text-decoration:none;display:inline-block;letter-spacing:.5px}
.btn:hover{opacity:.82}
.bg{background:#238636;color:#fff}.br{background:#b91c1c;color:#fff}
.bb{background:#1f6feb;color:#fff}.by{background:#d29922;color:#000}
.bgr{background:#21262d;color:#c9d1d9;border:1px solid #30363d}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600}
.g{background:#0d4429;color:#3fb950}.r{background:#3d0c0c;color:#f85149}
.y{background:#3d2e0c;color:#d29922}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:#8b949e;font-weight:500;text-align:left;padding:7px 9px;border-bottom:1px solid #21262d;
font-size:10px;letter-spacing:1.5px;text-transform:uppercase}
td{padding:8px 9px;border-bottom:1px solid #21262d;vertical-align:middle}
tr:last-child td{border-bottom:none}tr:hover td{background:#1c2128}
.mono{font-family:monospace;color:#58a6ff;font-size:11px}
.tbox{background:#0d2d1a;border:1px solid #238636;border-radius:8px;padding:14px 18px;margin-top:14px}
.tbig{font-family:monospace;font-size:18px;font-weight:700;color:#58a6ff;letter-spacing:2px;margin:5px 0}
nav{display:flex;gap:7px;margin-bottom:18px;flex-wrap:wrap;width:100%;max-width:980px}
.si{width:52px!important;display:inline!important;padding:3px 5px!important;font-size:10px!important}
.bsm{padding:4px 9px!important;font-size:10px!important}
.if{display:inline}
.ag{padding:10px 14px;border-radius:6px;margin-bottom:14px;font-size:12px;width:100%;max-width:980px}
.ag-g{background:#0d2d1a;border:1px solid #238636;color:#3fb950}
.ag-r{background:#2d0d0d;border:1px solid #b91c1c;color:#f85149}
.stat{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:18px;text-align:center}
.sn{font-size:26px;font-weight:900}.sl{font-size:10px;color:#8b949e;margin-top:3px}
</style>"""

def _shell(title,body,active=""):
    nl=[("Dashboard","/x7k2","d"),("Payments","/x7k2/payments","p"),
        ("Add License","/x7k2/add","a"),("All Licenses","/x7k2/licenses","l"),
        ("Logout","/x7k2/logout","x")]
    nav="<nav>"+"".join(
        '<a href="'+h+'" class="btn '+('bb' if k==active else 'bgr')+'">'+t+'</a>'
        for t,h,k in nl)+"</nav>"
    return ("<!DOCTYPE html><html><head><title>"+title+" — TBOT Admin</title>"
            +_CSS+"</head><body><h1>TBOT Admin</h1>"
            "<p class='sub'>License &amp; Payment Management</p>"+nav+body+"</body></html>")

# ══════════════════════════════════════════════════════════════════════════════
#  CLIENT ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("terminal.html")

@app.route("/ping")
def ping():
    """Lightweight endpoint for uptime monitors — keeps Render awake."""
    return "ok", 200


@app.route("/keepalive")
def keepalive():
    """
    Smart keepalive called by your cron job every minute.
    Does three things:
      1. Keeps the web server awake (same as /ping)
      2. Checks every running bot process — restarts any that died
      3. Returns a status report so you can see what is happening
    """
    now    = time.time()
    report = []
    dead   = []

    for name, bs in list(bot_sessions.items()):
        proc     = bs.get("proc")
        started  = bs.get("started_at", now)
        last_sid = bs.get("sid", "")
        uptime   = int(now - started)

        if proc is None:
            dead.append(name)
            report.append(f"{name}: no process")
            continue

        exit_code = proc.poll()  # None = still running
        if exit_code is not None:
            # Process died
            dead.append(name)
            report.append(f"{name}: DIED (exit={exit_code}) after {uptime}s")
            log.info(f"[keepalive] {name} bot died (exit={exit_code}) — will restart if user reconnects")
        else:
            # Still running — write a heartbeat so we know it is alive
            hb_file = os.path.join(BASE_DIR, f"heartbeat_{_safe_name(name)}.txt")
            try:
                with open(hb_file, "w") as f:
                    f.write(str(now))
            except Exception:
                pass
            report.append(f"{name}: running {uptime}s")

    # Clean up dead sessions from bot_sessions
    for name in dead:
        bot_sessions.pop(name, None)
        active_users.pop(name, None)

    active_count = len(bot_sessions)
    status = {
        "ok":           True,
        "ts":           now,
        "active_bots":  active_count,
        "dead_cleaned": len(dead),
        "bots":         report,
    }
    log.info(f"[keepalive] {active_count} bots alive, {len(dead)} cleaned")
    return jsonify(status), 200



_SIGNUP_CSS="""<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'IBM Plex Mono',monospace;background:#090c10;color:#c8d8e8;
min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#0e1318;border:1px solid #1a2130;border-radius:12px;padding:34px 38px;
width:100%;max-width:440px}
.logo{font-size:22px;font-weight:900;color:#fff;margin-bottom:3px}
.logo span{color:#00e5a0}
.sub{font-size:10px;letter-spacing:3px;color:#4a6070;text-transform:uppercase;margin-bottom:28px}
label{display:block;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#4a6070;margin-bottom:5px}
input,textarea{width:100%;background:rgba(255,255,255,.03);border:1px solid #1a2130;border-radius:6px;
padding:10px 13px;color:#c8d8e8;font-family:inherit;font-size:13px;outline:none;
margin-bottom:16px;transition:border-color .2s;resize:none}
input:focus,textarea:focus{border-color:#00e5a0}
.btn{width:100%;background:#00e5a0;color:#000;border:none;border-radius:8px;padding:12px;
font-size:13px;font-weight:900;letter-spacing:1.5px;text-transform:uppercase;cursor:pointer;transition:all .15s}
.btn:hover{background:#00ffb3}
.note{font-size:11px;color:#4a6070;text-align:center;margin-top:14px;line-height:1.6}
.wallet{background:rgba(0,229,160,.04);border:1px solid rgba(0,229,160,.2);border-radius:8px;
padding:14px 18px;margin-bottom:20px}
.wl{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#4a6070;margin-bottom:6px}
.wa{font-size:12px;color:#00e5a0;word-break:break-all;font-weight:600;cursor:pointer}
.nb{display:inline-block;background:rgba(0,229,160,.1);border:1px solid rgba(0,229,160,.2);
color:#00e5a0;font-size:9px;letter-spacing:2px;padding:3px 8px;border-radius:3px;margin-bottom:16px}
.cp{background:transparent;border:1px solid rgba(0,229,160,.3);color:#00e5a0;border-radius:4px;
padding:4px 10px;font-size:10px;cursor:pointer;margin-top:8px}
.amt{font-size:28px;font-weight:900;color:#00e5a0;margin-bottom:3px}
.ams{font-size:11px;color:#4a6070;margin-bottom:20px}
.inf{font-size:11px;color:#4a6070;line-height:1.7;margin-bottom:16px}
.hi{color:#c8d8e8}
</style>"""

@app.route("/signup")
def signup():
    return render_template("signup.html", price=PRICE_USD, days=LICENSE_DAYS)

@app.route("/signup",methods=["POST"])
def signup_post():
    name=request.form.get("name","").strip()
    email=request.form.get("email","").strip().lower()
    if not name or not email: return redirect("/signup")
    flask_session["sn"]=name; flask_session["se"]=email
    return redirect("/payment")

@app.route("/payment")
def payment():
    name=flask_session.get("sn",""); email=flask_session.get("se","")
    if not name or not email: return redirect("/signup")
    return render_template("payment.html",
        name=name, email=email,
        price=PRICE_USD, days=LICENSE_DAYS,
        wallet=USDT_ADDRESS or "NOT_CONFIGURED",
        admin_email=ADMIN_EMAIL or GMAIL_ADDRESS or "support@tbot.com",
    )

@app.route("/submit-proof",methods=["POST"])
def submit_proof():
    name=flask_session.get("sn",""); email=flask_session.get("se","")
    proof=request.form.get("proof_notes","").strip()
    if not name or not email: return redirect("/signup")
    c=_db()
    cur=c.execute("INSERT INTO pending_payments(name,email,proof_notes,submitted_at,status) VALUES(?,?,?,?,'pending')",
                  (name,email,proof,time.time()))
    pid=cur.lastrowid; c.commit(); c.close()
    es_key = f"notify_{pid}"
    _es_set(es_key, "pending")
    def _notify_admin():
        try:
            _email_admin_notify(name, email, proof, pid)
            log.info(f"[notify] Admin email sent to {ADMIN_EMAIL!r} for {name!r}")
            _es_set(es_key, "ok")
        except Exception as e:
            log.error(f"[notify] FAILED: {e}")
            _es_set(es_key, "error", str(e))
    threading.Thread(target=_notify_admin, daemon=True).start()
    flask_session.pop("sn",None); flask_session.pop("se",None)
    return render_template("pending.html", name=name, email=email, price=PRICE_USD, es_key=es_key)

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════


@app.route("/admin-path")
def show_admin_path():
    """Diagnostic — shows what admin path is active. Remove after confirming."""
    return f"Admin path is: {"/x7k2"} | Password set: {bool(ADMIN_PASSWORD)}"
@app.route("/x7k2/login",methods=["GET","POST"])
def admin_login():
    if not ADMIN_PASSWORD: return Response("Set ADMIN_PASSWORD env var.",403)
    err=""
    if request.method=="POST":
        if request.form.get("password")==ADMIN_PASSWORD:
            flask_session["admin_logged_in"]=True; return redirect("/x7k2")
        err="<p style='color:#f85149;font-size:13px;margin-bottom:10px'>Incorrect password.</p>"
    return ("<!DOCTYPE html><html><head><title>Admin Login</title><style>"
            "*{box-sizing:border-box;margin:0;padding:0}"
            "body{font-family:sans-serif;background:#0d1117;color:#c9d1d9;"
            "min-height:100vh;display:flex;align-items:center;justify-content:center}"
            "form{background:#161b22;border:1px solid #30363d;border-radius:10px;"
            "padding:30px;width:310px;display:flex;flex-direction:column;gap:10px}"
            "h2{color:#58a6ff;font-size:17px;text-align:center}"
            "input{background:#0d1117;border:1px solid #30363d;border-radius:6px;"
            "color:#c9d1d9;padding:9px 11px;font-size:13px;outline:none}"
            "button{background:#1f6feb;color:#fff;border:none;border-radius:6px;"
            "padding:9px;font-size:13px;font-weight:600;cursor:pointer}"
            "</style></head><body><form method='POST'>"
            "<h2>TBOT Admin</h2>"+err
            +"<input type='password' name='password' placeholder='Admin password' autofocus>"
            "<button>Sign In</button></form></body></html>")

@app.route("/x7k2/logout")
def admin_logout():
    flask_session.pop("admin_logged_in",None); return redirect("/x7k2/login")

@app.route("/x7k2")
@_admin_required
def admin_index():
    lics=_load_licenses(); now=time.time()
    active=sum(1 for l in lics if l.get("expiry",0)>now)
    pend=_pending_count()
    # Recent 5 payments
    c=_db()
    rows=c.execute("SELECT id,name,email,submitted_at,status FROM pending_payments ORDER BY submitted_at DESC LIMIT 5").fetchall()
    c.close()
    recent=[{"id":r[0],"name":r[1],"email":r[2],
             "date":datetime.fromtimestamp(r[3]).strftime("%Y-%m-%d %H:%M"),
             "status":r[4]} for r in rows]
    return render_template("admin.html",
        admin_path="/x7k2",
        page_title="Dashboard", active_page="dashboard",
        active_licenses=active, total_licenses=len(lics),
        expired_licenses=len(lics)-active, pending_count=pend,
        recent_payments=recent,
    )

@app.route("/x7k2/payments")
@_admin_required
def admin_payments():
    c=_db()
    rows=c.execute("SELECT id,name,email,proof_notes,submitted_at,status FROM pending_payments ORDER BY submitted_at DESC").fetchall()
    c.close()
    payments=[{"id":r[0],"name":r[1],"email":r[2],"proof":r[3],
               "date":datetime.fromtimestamp(r[4]).strftime("%Y-%m-%d %H:%M"),
               "status":r[5]} for r in rows]
    return render_template("admin.html",
        admin_path="/x7k2",
        page_title="Payments", active_page="payments",
        pending_count=_pending_count(), payments=payments,
        license_days=LICENSE_DAYS,
    )

@app.route("/x7k2/confirm-payment",methods=["POST"])
@_admin_required
def admin_confirm_payment():
    pid=request.form.get("payment_id")
    days=int(request.form.get("days",LICENSE_DAYS) or LICENSE_DAYS)
    c=_db()
    row=c.execute("SELECT name,email FROM pending_payments WHERE id=? AND status='pending'",(pid,)).fetchone()
    if not row: c.close(); return redirect("/x7k2/payments")
    name,email=row
    rec=_create_license(name,email,days)
    c.execute("UPDATE pending_payments SET status='confirmed' WHERE id=?",(pid,)); c.commit(); c.close()
    exp = datetime.fromtimestamp(rec["expiry"]).strftime("%Y-%m-%d")
    tok = rec["license_token"]
    es_key = f"license_{pid}"
    _es_set(es_key, "pending")
    def _deliver_license():
        try:
            _email_license(name, email, tok, rec["expiry"], days)
            log.info(f"[license] Email sent to {email!r}")
            _es_set(es_key, "ok")
        except Exception as e:
            log.error(f"[license] FAILED to email {email!r}: {e}")
            _es_set(es_key, "error", str(e))
    threading.Thread(target=_deliver_license, daemon=True).start()
    return render_template("admin.html",
        admin_path="/x7k2",
        page_title="Payment Confirmed", active_page="confirmed",
        pending_count=_pending_count(),
        confirmed={"name":name,"email":email,"token":tok,"expires":exp,"days":days},
        es_key=es_key,
    )

@app.route("/x7k2/reject-payment",methods=["POST"])
@_admin_required
def admin_reject_payment():
    pid=request.form.get("payment_id")
    c=_db(); c.execute("UPDATE pending_payments SET status='rejected' WHERE id=?",(pid,))
    c.commit(); c.close(); return redirect("/x7k2/payments")

@app.route("/x7k2/add",methods=["GET","POST"])
@_admin_required
def admin_add():
    created=None; email_sent=False; es_key=None
    if request.method=="POST":
        name=request.form.get("name","").strip(); email=request.form.get("email","").strip()
        days=int(request.form.get("days",LICENSE_DAYS) or LICENSE_DAYS)
        send=request.form.get("send_email")=="1"
        if name and email:
            rec=_create_license(name,email,days); tok=rec["license_token"]
            exp=datetime.fromtimestamp(rec["expiry"]).strftime("%Y-%m-%d")
            created={"name":name,"email":email,"token":tok,"expires":exp,"days":days}
            if send:
                email_sent=True
                es_key = f"add_{secrets.token_hex(6)}"
                _es_set(es_key, "pending")
                def _s(k=es_key):
                    try:
                        _email_license(name,email,tok,rec["expiry"],days)
                        _es_set(k, "ok")
                    except Exception as e:
                        print(f"[add] {e}")
                        _es_set(k, "error", str(e))
                threading.Thread(target=_s,daemon=True).start()
            else:
                es_key = None
    return render_template("admin.html",
        admin_path="/x7k2",
        page_title="Add License", active_page="add",
        pending_count=_pending_count(),
        created=created, email_sent=email_sent,
        license_days=LICENSE_DAYS,
        es_key=es_key if email_sent else None,
    )

@app.route("/x7k2/licenses")
@_admin_required
def admin_licenses():
    raw=_load_licenses(); now=time.time()
    active=sum(1 for l in raw if l.get("expiry",0)>now)
    lics=[{
        "id":    l["_id"],
        "name":  l.get("name",""),
        "email": l.get("email","—"),
        "token": l.get("license_token",""),
        "expires": datetime.fromtimestamp(l["expiry"]).strftime("%Y-%m-%d") if l.get("expiry") else "—",
        "days_left": int((l.get("expiry",0)-now)/86400),
    } for l in raw]
    return render_template("admin.html",
        admin_path="/x7k2",
        page_title="Licenses", active_page="licenses",
        pending_count=_pending_count(),
        licenses=lics, total_licenses=len(lics),
        active_licenses=active, expired_licenses=len(lics)-active,
    )

@app.route("/x7k2/revoke-license",methods=["POST"])
@_admin_required
def admin_revoke_license():
    lid=request.form.get("lid")
    if lid:
        c=_db(); c.execute("DELETE FROM licenses WHERE id=?",(lid,)); c.commit(); c.close()
    return redirect("/x7k2/licenses")

@app.route("/x7k2/extend-license",methods=["POST"])
@_admin_required
def admin_extend_license():
    lid=request.form.get("lid"); days=int(request.form.get("days",30) or 30)
    if lid:
        c=_db(); row=c.execute("SELECT data FROM licenses WHERE id=?",(lid,)).fetchone()
        if row:
            l=json.loads(row[0]); l["expiry"]=max(l.get("expiry",time.time()),time.time())+days*86400
            c.execute("UPDATE licenses SET data=? WHERE id=?",(json.dumps(l),lid))
            c.commit()
        c.close()
    return redirect("/x7k2/licenses")

# ══════════════════════════════════════════════════════════════════════════════
#  BOT ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/verify_and_save",methods=["POST"])
def verify_and_save():
    try:
        sid=request.headers.get("X-Socket-ID","")
        body=request.get_json(silent=True) or {}
        app_id=str(body.get("app_id","")).strip()
        api_token=str(body.get("api_token","")).strip()
        if not app_id: return jsonify({"ok":False,"error":"App ID required"}),400
        if not api_token: return jsonify({"ok":False,"error":"API Token required"}),400
        rq=queue.Queue()
        def _oo(ws): ws.send(json.dumps({"authorize":api_token}))
        def _om(ws,m): ws.close(); rq.put(("ok",m))
        def _oe(ws,e): rq.put(("error",str(e)))
        def _oc(ws,*a):
            if rq.empty(): rq.put(("error","Connection closed"))
        ws2=_ws_client.WebSocketApp(f"wss://ws.derivws.com/websockets/v3?app_id={app_id}",
            on_open=_oo,on_message=_om,on_error=_oe,on_close=_oc)
        t=threading.Thread(target=ws2.run_forever,kwargs={"ping_timeout":10},daemon=True)
        t.start()
        t.join(timeout=15)
        if rq.empty(): return jsonify({"ok":False,"error":"Deriv timed out — check App ID"}),408
        st,payload=rq.get()
        if st=="error": return jsonify({"ok":False,"error":f"Deriv: {payload}"}),400
        resp=json.loads(payload)
        if "error" in resp:
            cm={"InvalidToken":"Invalid API token.","InvalidAppID":"Invalid App ID.","RateLimit":"Rate limited."}
            ec=resp["error"].get("code","")
            return jsonify({"ok":False,"error":cm.get(ec,resp["error"].get("message","Auth failed"))}),401
        auth=resp.get("authorize",{})
        account=auth.get("fullname") or auth.get("email") or auth.get("loginid") or ""
        if sid:
            cfg=dict(DEFAULT_CONFIG); cfg["app_id"]=app_id; cfg["api_token"]=api_token; configs[sid]=cfg
        return jsonify({"ok":True,"account":account})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route("/save_config",methods=["POST"])
def save_config():
    try:
        sid=request.headers.get("X-Socket-ID",""); body=request.get_json(silent=True)
        if not body: return jsonify({"ok":False,"error":"No body"}),400
        ai=str(body.get("app_id","")).strip(); at=str(body.get("api_token","")).strip()
        if not ai or not at: return jsonify({"ok":False,"error":"Missing fields"}),400
        cfg=configs.get(sid,dict(DEFAULT_CONFIG)); cfg["app_id"]=ai; cfg["api_token"]=at; configs[sid]=cfg
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

@app.route("/get_config")
def get_config():
    return jsonify(configs.get(request.headers.get("X-Socket-ID",""),dict(DEFAULT_CONFIG)))



@app.route("/debug/email")
def debug_email():
    """
    Live email debugger — visit this URL in your browser to test email.
    Protected by ADMIN_PASSWORD as a query param.
    Example: /debug/email?key=YOUR_PASSWORD&to=test@gmail.com
    """
    key = request.args.get("key", "")
    if key != ADMIN_PASSWORD:
        return Response("Unauthorized — add ?key=YOUR_ADMIN_PASSWORD", 401)

    to = request.args.get("to", ADMIN_EMAIL or GMAIL_ADDRESS)

    lines = []
    lines.append("<pre style='font-family:monospace;background:#0d1117;color:#c9d1d9;padding:24px;font-size:13px;line-height:1.8'>")
    lines.append("<b style='color:#00e5a0'>TBOT Email Debugger</b>\n")
    lines.append(f"Time          : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"GMAIL_ADDRESS : {GMAIL_ADDRESS!r}")
    lines.append(f"GMAIL_APP_PASS: {'SET (' + str(len(GMAIL_APP_PASS)) + ' chars)' if GMAIL_APP_PASS else 'NOT SET ⚠'}")
    lines.append(f"ADMIN_EMAIL   : {ADMIN_EMAIL!r}")
    lines.append(f"APP_URL       : {APP_URL!r}")
    lines.append(f"FROM_NAME     : {FROM_NAME!r}")
    lines.append(f"Sending to    : {to!r}")
    lines.append("")

    if not GMAIL_ADDRESS:
        lines.append("<b style='color:#f85149'>✗ GMAIL_ADDRESS not set in environment variables</b>")
        lines.append("</pre>")
        return "\n".join(lines)

    if not GMAIL_APP_PASS:
        lines.append("<b style='color:#f85149'>✗ GMAIL_APP_PASS not set in environment variables</b>")
        lines.append("Go to Render Dashboard → Environment → add GMAIL_APP_PASS")
        lines.append("Generate one at: myaccount.google.com/apppasswords")
        lines.append("</pre>")
        return "\n".join(lines)

    # Test SSL port 465 (only method used by the app)
    lines.append("Testing SSL port 465...")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        lines.append("<b style='color:#3fb950'>✓ SSL-465 auth OK</b>")
        ssl_ok = True
    except smtplib.SMTPAuthenticationError as e:
        lines.append(f"<b style='color:#f85149'>✗ Auth FAILED: {e}</b>")
        ssl_ok = False
    except Exception as e:
        lines.append(f"<b style='color:#d29922'>⚠ Connection error: {e}</b>")
        ssl_ok = False

    lines.append("")

    if not ssl_ok:
        lines.append("<b style='color:#f85149'>✗ Auth failed — cannot send email</b>")
        lines.append("")
        lines.append("Common fixes:")
        lines.append("  1. Make sure 2-Step Verification is ON for the Gmail account")
        lines.append("  2. Generate a fresh App Password at: myaccount.google.com/apppasswords")
        lines.append("  3. Copy the 16-char password exactly (no spaces)")
        lines.append("  4. Update GMAIL_APP_PASS in Render dashboard → Save → redeploy")
        lines.append("  5. Make sure the Gmail account is not locked or suspended")
        lines.append("</pre>")
        return "\n".join(lines)

    # Auth worked — now actually send a test email
    lines.append(f"Sending test email to {to!r}...")
    try:
        _send_email(
            to,
            "TBOT Email Test",
            f"<div style='font-family:sans-serif;padding:24px'>"
            f"<h2 style='color:#00e5a0'>Email is working ✓</h2>"
            f"<p>This test was sent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
            f"<p>GMAIL_ADDRESS: {GMAIL_ADDRESS}</p></div>",
            f"TBOT email test sent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nGMAIL: {GMAIL_ADDRESS}"
        )
        lines.append(f"<b style='color:#3fb950'>✓ Test email sent to {to} — check inbox (and spam folder)</b>")
        log.info(f"[debug/email] Test email sent to {to}")
    except Exception as e:
        lines.append(f"<b style='color:#f85149'>✗ Send failed: {e}</b>")
        log.error(f"[debug/email] Send failed: {e}")

    lines.append("</pre>")
    return "\n".join(lines)

@app.route("/email-status/<key>")
def email_status(key):
    """Browser polls this to get the result of a background email send."""
    return jsonify(_es_get(key))

@app.route("/session-status")
def session_status():
    """Browser polls this after page load to check if bot is still running."""
    sid  = request.headers.get("X-Socket-ID","")
    name = request.args.get("name","")
    bs   = bot_sessions.get(name)
    if bs and bs.get("proc") and bs["proc"].poll() is None:
        return jsonify({"running": True,  "started_at": bs.get("started_at",0)})
    return jsonify({"running": False})


@socketio.on("check_license")
def check_license(data):
    v,m=validate_license(data.get("name",""),data.get("token",""))
    emit("license_result",{"valid":v,"msg":m})

@socketio.on("start_session")
def start_session(data):
    sid=request.sid; name=data["name"]; token=data["token"]
    v,m=validate_license(name,token)
    if not v: emit("terminal_output",f"\r\n{m}\r\n"); return

    # ── One session per license name ──────────────────────────────────────
    # If this user is already connected on another browser/tab, block them
    existing_sid = active_users.get(name)
    if existing_sid and existing_sid != sid and existing_sid in sessions:
        emit("terminal_output",
             "\r\n[ERROR] This license is already active in another session.\r\n"
             "[ERROR] Only one connection per license is allowed.\r\n"
             "[ERROR] Disconnect the other session first.\r\n")
        return

    if sid in sessions: _detach_socket(sid)
    names[sid]        = name
    active_users[name] = sid

    safe = _safe_name(name)

    # ── Check if bot is already running for this user ─────────────────────────
    existing = bot_sessions.get(name)
    if existing and existing.get("proc") and existing["proc"].poll() is None:
        # Bot still running — just attach this socket to it
        sessions[sid] = existing["pipe"]
        procs[sid]    = existing["proc"]
        # Replay last buffer so user sees what happened while away
        prior = existing.get("buffer","")
        if prior:
            emit("terminal_output", f"\r\n[Reconnected — replaying last output]\r\n{prior}")
        else:
            emit("terminal_output", f"\r\n[Reconnected to running bot]\r\n")
        existing["sid"] = sid  # update which socket is watching
        socketio.start_background_task(read_terminal, sid)
        log.info(f"[session] {name} reconnected to existing bot")
        return

    # ── No existing bot — start a fresh one ───────────────────────────────────
    ucp = os.path.join(BASE_DIR, f"config_{safe}.json")
    with open(ucp,"w") as f:
        json.dump(configs.get(sid, dict(DEFAULT_CONFIG)), f, indent=4)

    script = os.path.join(BASE_DIR,"5min_Tbot.py")
    python = sys.executable or "python3"

    env = os.environ.copy()
    env["TBOT_USER"]        = safe
    env["TBOT_SID"]         = sid
    env["TBOT_CONFIG"]      = ucp
    env["TBOT_PID_FILE"]    = os.path.join(BASE_DIR, f"bot_{safe}.pid")
    env["TBOT_DATA_DIR"]    = BASE_DIR
    env["TBOT_CMD_FILE"]    = os.path.join(BASE_DIR, f"cmd_{safe}.json")
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            [python, script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=BASE_DIR,
            env=env,
            bufsize=0,
        )
        sessions[sid]    = proc.stdout
        procs[sid]       = proc
        buffers[sid]     = ""
        # Store in bot_sessions keyed by name so reconnects can find it
        bot_sessions[name] = {
            "proc":       proc,
            "pipe":       proc.stdout,
            "buffer":     "",
            "sid":        sid,
            "safe_name":  safe,
            "started_at": time.time(),
            "last_seen":  time.time(),
        }
        socketio.start_background_task(read_terminal, sid)
        socketio.start_background_task(_idle_watchdog, name)
        emit("terminal_output", f"\r\n[Bot session started]\r\n")
        log.info(f"[session] {name} started new bot")
    except Exception as e:
        emit("terminal_output", f"\r\n[LAUNCH ERROR] {e}\r\n")

def read_terminal(sid):
    while True:
        pipe = sessions.get(sid)
        proc = procs.get(sid)
        if not pipe or not proc:
            break
        try:
            chunk = pipe.read(1024)   # blocks until data available
        except Exception:
            break
        if not chunk:
            # EOF — process has exited
            socketio.emit("terminal_output", "\r\n[process exited]\r\n", to=sid)
            _cleanup_session(sid)
            break
        text = chunk.decode(errors="ignore")
        buffers[sid] = (buffers.get(sid,"") + text)[-20000:]
        socketio.emit("terminal_output", text, to=sid)
        # Also store in bot_sessions buffer so reconnects can replay it
        name = names.get(sid)
        if name and name in bot_sessions:
            bs = bot_sessions[name]
            bs["buffer"] = (bs.get("buffer","") + text)[-BUFFER_SIZE:]

def _detach_socket(sid):
    """Disconnect a socket from its bot WITHOUT killing the bot process.
    Called on browser disconnect/refresh — bot keeps running."""
    pipe = sessions.pop(sid, None)
    proc = procs.pop(sid, None)
    # DON'T close pipe or kill proc — bot keeps running
    # Just update bot_sessions last_seen time
    name = names.get(sid)
    if name and name in bot_sessions:
        bot_sessions[name]["last_seen"] = time.time()
    buffers.pop(sid, None)
    configs.pop(sid, None)
    name = names.pop(sid, None)
    if name and active_users.get(name) == sid:
        active_users.pop(name, None)


def _idle_watchdog(name):
    """Background task — kills the bot if user has been disconnected too long."""
    while True:
        time.sleep(60)
        bs = bot_sessions.get(name)
        if not bs:
            break
        proc = bs.get("proc")
        if not proc or proc.poll() is not None:
            # Process already dead — clean up
            bot_sessions.pop(name, None)
            break
        # If user is currently connected, reset timer
        if active_users.get(name):
            bs["last_seen"] = time.time()
            continue
        # User is disconnected — check how long
        idle = time.time() - bs.get("last_seen", time.time())
        if idle > IDLE_TIMEOUT:
            print(f"[watchdog] {name} idle {idle:.0f}s — killing bot", flush=True)
            try: proc.terminate()
            except: pass
            try: proc.wait(timeout=3)
            except:
                try: proc.kill()
                except: pass
            bot_sessions.pop(name, None)
            break


def _cleanup_session(sid):
    pipe = sessions.pop(sid, None)
    proc = procs.pop(sid, None)
    buffers.pop(sid, None)
    configs.pop(sid, None)
    name = names.pop(sid, None)
    if name and active_users.get(name) == sid:
        active_users.pop(name, None)
    # Remove from bot_sessions — this is a full stop
    if name:
        bot_sessions.pop(name, None)

    # Close the output pipe
    if pipe:
        try: pipe.close()
        except Exception: pass

    # Terminate the bot process
    if proc:
        try: proc.terminate()
        except Exception: pass
        try: proc.wait(timeout=3)
        except Exception:
            try: proc.kill()
            except Exception: pass

    # Remove temp config file
    ucp = os.path.join(BASE_DIR, f"config_{sid}.json")
    if os.path.exists(ucp):
        try: os.remove(ucp)
        except OSError: pass

    # Remove pid file
    bpf = os.path.join(BASE_DIR, f"bot_{sid}.pid")
    if os.path.exists(bpf):
        try: os.remove(bpf)
        except OSError: pass

    # Remove command file
    cmdf = os.path.join(BASE_DIR, f"cmd_{sid}.json")
    if os.path.exists(cmdf):
        try: os.remove(cmdf)
        except OSError: pass

@socketio.on("terminal_input")
def terminal_input(data):
    # Legacy PTY keypress — not used in subprocess mode
    pass


@socketio.on("send_command")
def send_command(data):
    """
    Write a command to the bot's command file (keyed by safe user name).
    The bot's _watch_cmd_file() thread picks it up within 1 second.
    """
    sid  = request.sid
    name = names.get(sid)

    if not name:
        emit("command_ack", {"ok": False, "error": "No active session"})
        return

    # Must match TBOT_CMD_FILE set in start_session env: cmd_{safe_name}.json
    safe     = _safe_name(name)
    cmd_file = os.path.join(BASE_DIR, f"cmd_{safe}.json")

    try:
        with open(cmd_file, "w") as f:
            json.dump(data, f)
        log.info(f"[cmd] wrote {data.get('action','?')} to {cmd_file}")
        emit("command_ack", {"ok": True, "action": data.get("action","")})
    except Exception as e:
        log.error(f"[cmd] failed to write cmd file: {e}")
        emit("command_ack", {"ok": False, "error": str(e)})

@socketio.on("stop_session")
def stop_session(data):
    _cleanup_session(request.sid); emit("terminal_output","\r\nSESSION STOPPED\r\n")

@socketio.on("disconnect")
def on_disconnect():
    """Browser closed or refreshed — keep bot running, just detach socket."""
    sid = request.sid
    name = names.get(sid, "?")
    print(f"[disconnect] {name} disconnected (sid={sid[:8]}...) — bot kept alive", flush=True)
    _detach_socket(sid)



@app.route("/x7k2/memory")
@_admin_required
def admin_memory():
    """Memory management page — view, download and upload win/loss training data."""
    win_path  = os.path.join(BASE_DIR, "win.json")
    loss_path = os.path.join(BASE_DIR, "loss.json")
    snap_path = os.path.join(BASE_DIR, "memory_snapshot.json")

    def _count(path):
        try:
            with open(path) as f: return len(json.load(f))
        except: return 0

    def _size(path):
        if not os.path.exists(path): return "—"
        b = os.path.getsize(path)
        return f"{b/1024:.1f} KB" if b > 1024 else f"{b} B"

    def _modified(path):
        if not os.path.exists(path): return "—"
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")

    files = []
    for name, path in [("win.json", win_path), ("loss.json", loss_path),
                        ("memory_snapshot.json", snap_path)]:
        files.append({
            "name":     name,
            "count":    _count(path) if path.endswith(".json") and "snapshot" not in name else "—",
            "size":     _size(path),
            "modified": _modified(path),
        })

    memory = {
        "win_count":  _count(win_path),
        "loss_count": _count(loss_path),
        "files":      files,
        "flash":      flask_session.pop("mem_flash", None),
        "error":      flask_session.pop("mem_error", None),
    }

    return render_template("admin.html",
        page_title="Memory", active_page="memory",
        pending_count=_pending_count(),
        admin_path="/x7k2",
        memory=memory,
    )


@app.route("/x7k2/memory/download")
@_admin_required
def admin_memory_download():
    """Download win.json, loss.json or memory_snapshot.json."""
    file_key = request.args.get("file", "win")
    name_map = {
        "win":      "win.json",
        "loss":     "loss.json",
        "snapshot": "memory_snapshot.json",
    }
    fname = name_map.get(file_key, "win.json")
    fpath = os.path.join(BASE_DIR, fname)

    if not os.path.exists(fpath):
        flask_session["mem_error"] = f"{fname} not found — no data yet."
        return redirect("/x7k2" + "/memory")

    from flask import send_file as _sf
    return _sf(fpath, as_attachment=True, download_name=fname,
                mimetype="application/json")


@app.route("/x7k2/memory/upload", methods=["POST"])
@_admin_required
def admin_memory_upload():
    """Upload win.json or loss.json to replace current shared memory."""
    f = request.files.get("memfile")
    if not f:
        flask_session["mem_error"] = "No file selected."
        return redirect("/x7k2" + "/memory")

    fname = f.filename.lower().strip()
    if fname not in ("win.json", "loss.json"):
        flask_session["mem_error"] = "File must be named win.json or loss.json exactly."
        return redirect("/x7k2" + "/memory")

    try:
        content = f.read()
        data    = json.loads(content)
        if not isinstance(data, list):
            flask_session["mem_error"] = "Invalid format — file must be a JSON array."
            return redirect("/x7k2" + "/memory")
    except Exception as e:
        flask_session["mem_error"] = f"Invalid JSON: {e}"
        return redirect("/x7k2" + "/memory")

    fpath = os.path.join(BASE_DIR, fname)
    tmp   = fpath + ".tmp"
    with open(tmp, "wb") as out:
        out.write(content)
    os.replace(tmp, fpath)

    log.info(f"[memory] {fname} uploaded via admin — {len(data)} entries")
    flask_session["mem_flash"] = f"✓ {fname} uploaded successfully — {len(data)} setups loaded."
    return redirect("/x7k2" + "/memory")


@app.route("/x7k2/resend-license", methods=["POST"])
@_admin_required
def admin_resend_license():
    """Manually resend a license email — used when automatic send failed."""
    name    = request.form.get("name",    "").strip()
    email   = request.form.get("email",   "").strip()
    token   = request.form.get("token",   "").strip()
    days    = int(request.form.get("days",    LICENSE_DAYS) or LICENSE_DAYS)
    expires = request.form.get("expires", "")

    if not all([name, email, token]):
        flask_session["resend_error"] = "Missing name, email or token."
        return redirect("/x7k2" + "/licenses")

    # Rebuild expiry timestamp from date string
    try:
        expiry_ts = datetime.strptime(expires, "%Y-%m-%d").timestamp() + 86399
    except Exception:
        expiry_ts = time.time() + days * 86400

    try:
        _email_license(name, email, token, expiry_ts, days)
        log.info(f"[resend] License emailed to {email!r} for {name!r}")
        flask_session["resend_ok"] = f"License resent to {email}"
    except Exception as e:
        log.error(f"[resend] FAILED for {email!r}: {e}")
        flask_session["resend_error"] = f"Email failed: {e}"

    # Show result on confirmed page
    exp_fmt = expires or datetime.fromtimestamp(expiry_ts).strftime("%Y-%m-%d")
    return render_template("admin.html",
        admin_path="/x7k2",
        page_title="Payment Confirmed", active_page="confirmed",
        pending_count=_pending_count(),
        confirmed={"name":name,"email":email,"token":token,
                   "expires":exp_fmt,"days":days},
    )


@app.route("/x7k2/db/download")
@_admin_required
def admin_db_download():
    """Download the live license.db file for backup or recommit to GitHub."""
    db_path = os.path.join(BASE_DIR, "license.db")
    if not os.path.exists(db_path):
        flask_session["db_error"] = "license.db not found."
        return redirect("/x7k2" + "/db")
    from flask import send_file as _sf
    return _sf(db_path, as_attachment=True,
               download_name="license.db",
               mimetype="application/octet-stream")


@app.route("/x7k2/db", methods=["GET", "POST"])
@_admin_required
def admin_db():
    """Database manager — download live DB or upload a backup."""
    db_path = os.path.join(BASE_DIR, "license.db")

    flash = flask_session.pop("db_flash", None)
    error = flask_session.pop("db_error", None)

    if request.method == "POST":
        f = request.files.get("dbfile")
        if not f:
            flask_session["db_error"] = "No file selected."
            return redirect("/x7k2" + "/db")
        if not f.filename.lower().endswith(".db"):
            flask_session["db_error"] = "File must be a .db file."
            return redirect("/x7k2" + "/db")

        content = f.read()

        # Validate it is a real SQLite file (starts with SQLite magic bytes)
        if not content.startswith(b"SQLite format 3"):
            flask_session["db_error"] = "Invalid SQLite file — upload rejected."
            return redirect("/x7k2" + "/db")

        # Write atomically
        tmp = db_path + ".tmp"
        with open(tmp, "wb") as out:
            out.write(content)
        os.replace(tmp, db_path)

        log.info(f"[db] license.db uploaded via admin — {len(content):,} bytes")
        flask_session["db_flash"] = f"✓ license.db uploaded ({len(content):,} bytes). Database replaced."
        return redirect("/x7k2" + "/db")

    # GET — show the page
    db_size     = f"{os.path.getsize(db_path):,} bytes" if os.path.exists(db_path) else "not found"
    db_modified = (datetime.fromtimestamp(os.path.getmtime(db_path)).strftime("%Y-%m-%d %H:%M")
                   if os.path.exists(db_path) else "—")

    # Count licenses and pending payments
    try:
        c     = _db()
        n_lic = c.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        n_pay = c.execute("SELECT COUNT(*) FROM pending_payments").fetchone()[0]
        c.close()
    except Exception:
        n_lic = n_pay = "?"

    return render_template("admin.html",
        admin_path="/x7k2",
        page_title="Database", active_page="db",
        pending_count=_pending_count(),
        db={"size": db_size, "modified": db_modified,
            "licenses": n_lic, "payments": n_pay,
            "flash": flash, "error": error},
    )

# Admin routes registered directly under /x7k2

# Startup confirmation — visible in Render logs
log.info(f"[TBOT] Starting — async_mode={_async_mode} BASE_DIR={BASE_DIR}")
log.info("[TBOT] Admin URL: /x7k2")
log.info(f"[TBOT] ADMIN_PASSWORD set: {bool(ADMIN_PASSWORD)}")
log.info(f"[TBOT] GMAIL_ADDRESS={GMAIL_ADDRESS!r}")
log.info(f"[TBOT] GMAIL_APP_PASS set: {bool(GMAIL_APP_PASS)}")
log.info(f"[TBOT] ADMIN_EMAIL={ADMIN_EMAIL!r}")
log.info(f"[TBOT] APP_URL={APP_URL!r}")

if __name__=="__main__":
    port = int(os.environ.get("PORT", 10000))
    import socket as _s
    try: lip=_s.gethostbyname(_s.gethostname())
    except: lip="localhost"
    print(f"\n  T-BOT  →  http://localhost:5000")
    print(f"  Admin  →  http://localhost:5000/admin")
    print(f"  Signup →  http://localhost:5000/signup\n")
    socketio.run(app,host="0.0.0.0",port=port,debug=False,use_reloader=False)
