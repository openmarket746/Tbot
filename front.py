import os, sys, json, sqlite3, time, errno, subprocess
import threading, queue, secrets, string, smtplib
import websocket as _ws_client
import signal as _signal
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, jsonify,
                   session as flask_session, redirect, Response)
from flask_socketio import SocketIO, emit

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
# ADMIN_PATH: set this to a secret slug in Render env vars.
# Users will never know this URL exists. e.g. /x7k2-manage
# Must start with / and never be empty — falls back to /admin if not set or blank
_raw_admin_path = os.environ.get("ADMIN_PATH", "").strip().rstrip("/")
ADMIN_PATH      = _raw_admin_path if _raw_admin_path.startswith("/") else "/admin"

DEFAULT_CONFIG = {"app_id":"","api_token":"","pairs":[
    {"symbol":"R_100","trend":None},{"symbol":"R_50","trend":None},
    {"symbol":"R_25","trend":None},{"symbol":"R_10","trend":None},
    {"symbol":"R_5","trend":None}]}

sessions={}; procs={}; buffers={}; configs={}; names={}

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
def _send_email(to,subject,html,text):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASS:
        print(f"[email] ERROR: GMAIL_ADDRESS or GMAIL_APP_PASS not set", flush=True)
        raise ValueError("GMAIL_ADDRESS and GMAIL_APP_PASS not configured.")
    print(f"[email] Sending to={to!r} subject={subject!r} from={GMAIL_ADDRESS!r}", flush=True)
    msg=MIMEMultipart("alternative")
    msg["Subject"]=subject; msg["From"]=f"{FROM_NAME} <{GMAIL_ADDRESS}>"; msg["To"]=to
    msg.attach(MIMEText(text,"plain")); msg.attach(MIMEText(html,"html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com",465,timeout=30) as s:
            s.login(GMAIL_ADDRESS,GMAIL_APP_PASS)
            s.sendmail(GMAIL_ADDRESS,to,msg.as_string())
        print(f"[email] Sent OK to {to}", flush=True)
    except smtplib.SMTPAuthenticationError as e:
        print(f"[email] AUTH FAILED: {e} — check GMAIL_APP_PASS", flush=True)
        raise
    except Exception as e:
        print(f"[email] FAILED: {e}", flush=True)
        raise

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
        if not flask_session.get("admin_logged_in"): return redirect(ADMIN_PATH+"/login")
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
    nl=[("Dashboard",ADMIN_PATH,"d"),("Payments",ADMIN_PATH+"/payments","p"),
        ("Add License",ADMIN_PATH+"/add","a"),("All Licenses",ADMIN_PATH+"/licenses","l"),
        ("Logout",ADMIN_PATH+"/logout","x")]
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
    def _n():
        try: _email_admin_notify(name,email,proof,pid)
        except Exception as e: print(f"[notify] {e}")
    threading.Thread(target=_n,daemon=True).start()
    flask_session.pop("sn",None); flask_session.pop("se",None)
    return render_template("pending.html", name=name, email=email, price=PRICE_USD)

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route(ADMIN_PATH+"/login",methods=["GET","POST"])
def admin_login():
    if not ADMIN_PASSWORD: return Response("Set ADMIN_PASSWORD env var.",403)
    err=""
    if request.method=="POST":
        if request.form.get("password")==ADMIN_PASSWORD:
            flask_session["admin_logged_in"]=True; return redirect(ADMIN_PATH)
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

@app.route(ADMIN_PATH+"/logout")
def admin_logout():
    flask_session.pop("admin_logged_in",None); return redirect(ADMIN_PATH+"/login")

@app.route(ADMIN_PATH)
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
        admin_path=ADMIN_PATH,
        page_title="Dashboard", active_page="dashboard",
        active_licenses=active, total_licenses=len(lics),
        expired_licenses=len(lics)-active, pending_count=pend,
        recent_payments=recent,
    )

@app.route(ADMIN_PATH+"/payments")
@_admin_required
def admin_payments():
    c=_db()
    rows=c.execute("SELECT id,name,email,proof_notes,submitted_at,status FROM pending_payments ORDER BY submitted_at DESC").fetchall()
    c.close()
    payments=[{"id":r[0],"name":r[1],"email":r[2],"proof":r[3],
               "date":datetime.fromtimestamp(r[4]).strftime("%Y-%m-%d %H:%M"),
               "status":r[5]} for r in rows]
    return render_template("admin.html",
        admin_path=ADMIN_PATH,
        page_title="Payments", active_page="payments",
        pending_count=_pending_count(), payments=payments,
        license_days=LICENSE_DAYS,
    )

@app.route(ADMIN_PATH+"/confirm-payment",methods=["POST"])
@_admin_required
def admin_confirm_payment():
    pid=request.form.get("payment_id")
    days=int(request.form.get("days",LICENSE_DAYS) or LICENSE_DAYS)
    c=_db()
    row=c.execute("SELECT name,email FROM pending_payments WHERE id=? AND status='pending'",(pid,)).fetchone()
    if not row: c.close(); return redirect(ADMIN_PATH+"/payments")
    name,email=row
    rec=_create_license(name,email,days)
    c.execute("UPDATE pending_payments SET status='confirmed' WHERE id=?",(pid,)); c.commit(); c.close()
    def _d():
        try: _email_license(name,email,rec["license_token"],rec["expiry"],days)
        except Exception as e: print(f"[deliver] {e}")
    threading.Thread(target=_d,daemon=True).start()
    exp=datetime.fromtimestamp(rec["expiry"]).strftime("%Y-%m-%d")
    tok=rec["license_token"]
    return render_template("admin.html",
        admin_path=ADMIN_PATH,
        page_title="Payment Confirmed", active_page="confirmed",
        pending_count=_pending_count(),
        confirmed={"name":name,"email":email,"token":tok,"expires":exp,"days":days},
    )

@app.route(ADMIN_PATH+"/reject-payment",methods=["POST"])
@_admin_required
def admin_reject_payment():
    pid=request.form.get("payment_id")
    c=_db(); c.execute("UPDATE pending_payments SET status='rejected' WHERE id=?",(pid,))
    c.commit(); c.close(); return redirect(ADMIN_PATH+"/payments")

@app.route(ADMIN_PATH+"/add",methods=["GET","POST"])
@_admin_required
def admin_add():
    created=None; email_sent=False
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
                def _s():
                    try: _email_license(name,email,tok,rec["expiry"],days)
                    except Exception as e: print(f"[add] {e}")
                threading.Thread(target=_s,daemon=True).start()
    return render_template("admin.html",
        admin_path=ADMIN_PATH,
        page_title="Add License", active_page="add",
        pending_count=_pending_count(),
        created=created, email_sent=email_sent,
        license_days=LICENSE_DAYS,
    )

@app.route(ADMIN_PATH+"/licenses")
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
        admin_path=ADMIN_PATH,
        page_title="Licenses", active_page="licenses",
        pending_count=_pending_count(),
        licenses=lics, total_licenses=len(lics),
        active_licenses=active, expired_licenses=len(lics)-active,
    )

@app.route(ADMIN_PATH+"/revoke-license",methods=["POST"])
@_admin_required
def admin_revoke_license():
    lid=request.form.get("lid")
    if lid:
        c=_db(); c.execute("DELETE FROM licenses WHERE id=?",(lid,)); c.commit(); c.close()
    return redirect(ADMIN_PATH+"/licenses")

@app.route(ADMIN_PATH+"/extend-license",methods=["POST"])
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
    return redirect(ADMIN_PATH+"/licenses")

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

@socketio.on("check_license")
def check_license(data):
    v,m=validate_license(data.get("name",""),data.get("token",""))
    emit("license_result",{"valid":v,"msg":m})

@socketio.on("start_session")
def start_session(data):
    sid=request.sid; name=data["name"]; token=data["token"]
    v,m=validate_license(name,token)
    if not v: emit("terminal_output",f"\r\n{m}\r\n"); return
    if sid in sessions: _cleanup_session(sid)
    names[sid]=name

    # Write per-user config file
    ucp=os.path.join(BASE_DIR,f"config_{sid}.json")
    with open(ucp,"w") as f: json.dump(configs.get(sid,dict(DEFAULT_CONFIG)),f,indent=4)

    script=os.path.join(BASE_DIR,"Tbot_trend.py")
    python=sys.executable or "python3"

    env=os.environ.copy()
    env["TBOT_USER"]     = _safe_name(name)
    env["TBOT_SID"]      = sid
    env["TBOT_CONFIG"]   = ucp
    env["TBOT_PID_FILE"] = os.path.join(BASE_DIR,f"bot_{sid}.pid")
    env["TBOT_DATA_DIR"] = BASE_DIR
    env["TBOT_CMD_FILE"] = os.path.join(BASE_DIR,f"cmd_{sid}.json")
    env["PYTHONUNBUFFERED"] = "1"   # essential — ensures output streams immediately

    try:
        proc = subprocess.Popen(
            [python, script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout
            cwd=BASE_DIR,
            env=env,
            bufsize=0,                  # unbuffered — critical for real-time output
        )
        sessions[sid] = proc.stdout    # store the pipe, not a fd
        procs[sid]    = proc
        buffers[sid]  = ""
        socketio.start_background_task(read_terminal, sid)
        emit("terminal_output", f"\r\n[Session started]\r\n")
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

def _cleanup_session(sid):
    pipe = sessions.pop(sid, None)
    proc = procs.pop(sid, None)
    buffers.pop(sid, None)
    configs.pop(sid, None)
    names.pop(sid, None)

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
    Write a command to the bot's command file.
    The bot's _watch_cmd_file() thread picks it up within 1 second.

    Supported payloads:
      { action: "start",  stake_mode, stake, max_tp, max_sl,
                          max_open_trades, max_symbols }   — startup settings
      { action: "stake",  value: <float> }
      { action: "tp",     value: <float> }
      { action: "sl",     value: <float> }
      { action: "stop" }
    """
    sid = request.sid
    cmd_file = os.path.join(BASE_DIR, f"cmd_{sid}.json")
    try:
        with open(cmd_file, "w") as f:
            json.dump(data, f)
        emit("command_ack", {"ok": True, "action": data.get("action","")})
    except Exception as e:
        emit("command_ack", {"ok": False, "error": str(e)})

@socketio.on("stop_session")
def stop_session(data):
    _cleanup_session(request.sid); emit("terminal_output","\r\nSESSION STOPPED\r\n")

@socketio.on("disconnect")
def on_disconnect():
    sid=request.sid
    if sid in sessions: _cleanup_session(sid)

# Startup confirmation — visible in Render logs
print(f"[TBOT] Starting — async_mode={_async_mode} BASE_DIR={BASE_DIR}", flush=True)
print(f"[TBOT] ADMIN_PATH={ADMIN_PATH}", flush=True)
print(f"[TBOT] GMAIL configured: {bool(GMAIL_ADDRESS and GMAIL_APP_PASS)}", flush=True)

if __name__=="__main__":
    import socket as _s
    try: lip=_s.gethostbyname(_s.gethostname())
    except: lip="localhost"
    print(f"\n  T-BOT  →  http://localhost:5000")
    print(f"  Admin  →  http://localhost:5000/admin")
    print(f"  Signup →  http://localhost:5000/signup\n")
    socketio.run(app,host="0.0.0.0",port=5000,debug=False,use_reloader=False)
