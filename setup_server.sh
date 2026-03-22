#!/bin/bash
# ============================================================
#  T-BOT — Oracle Cloud / Ubuntu 22.04 Setup Script
#  Run this once on a fresh server as root or with sudo.
#
#  Usage:
#    chmod +x setup_server.sh
#    sudo ./setup_server.sh yourdomain.com
#
#  If you don't have a domain yet, pass your server's IP:
#    sudo ./setup_server.sh 123.456.78.90
# ============================================================

set -e

DOMAIN="${1:-localhost}"
APP_DIR="/app"
APP_USER="tbot"
PYTHON="python3"

echo ""
echo "================================================"
echo "  T-BOT Server Setup"
echo "  Domain/IP : $DOMAIN"
echo "  App dir   : $APP_DIR"
echo "================================================"
echo ""

# ── 1. System packages ───────────────────────────────────────
echo "[1/8] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    nginx \
    certbot python3-certbot-nginx \
    ufw \
    git curl wget

# ── 2. Firewall ───────────────────────────────────────────────
echo "[2/8] Configuring firewall..."
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

# Oracle Cloud also has a VCN security list — open ports 80 and 443 there too.
# Also open them in iptables (Oracle blocks by default at OS level):
iptables  -I INPUT  6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
iptables  -I INPUT  7 -m state --state NEW -p tcp --dport 443 -j ACCEPT
iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
netfilter-persistent save 2>/dev/null || true

# ── 3. App directory + user ───────────────────────────────────
echo "[3/8] Creating app directory and user..."
mkdir -p "$APP_DIR"

if ! id "$APP_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /bin/false "$APP_USER"
fi

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# ── 4. Python virtualenv + dependencies ──────────────────────
echo "[4/8] Installing Python dependencies..."
$PYTHON -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet \
    flask \
    flask-socketio \
    gevent \
    gevent-websocket \
    websocket-client \
    gunicorn \
    colorama

# ── 5. Nginx config ───────────────────────────────────────────
echo "[5/8] Writing Nginx config..."
cat > /etc/nginx/sites-available/tbot << NGINX
server {
    listen 80;
    server_name $DOMAIN;

    # Disable response buffering — essential for live terminal streaming
    proxy_buffering    off;
    proxy_cache        off;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_http_version 1.1;

        # Required for Socket.IO WebSocket upgrade
        proxy_set_header   Upgrade    \$http_upgrade;
        proxy_set_header   Connection "upgrade";

        proxy_set_header   Host            \$host;
        proxy_set_header   X-Real-IP       \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;

        # Keep long-lived socket connections alive (24 hours)
        proxy_read_timeout    86400;
        proxy_send_timeout    86400;
        proxy_connect_timeout 10;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/tbot /etc/nginx/sites-enabled/tbot
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# ── 6. Systemd service ────────────────────────────────────────
echo "[6/8] Writing systemd service..."
cat > /etc/systemd/system/tbot.service << SERVICE
[Unit]
Description=T-BOT Trading App
After=network.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn \\
    --worker-class gevent \\
    -w 1 \\
    --bind 127.0.0.1:5000 \\
    --timeout 300 \\
    front:app
Restart=always
RestartSec=5
User=$APP_USER
Group=$APP_USER

# Allow many open file descriptors (one per user PTY session)
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable tbot

# ── 7. HTTPS (only if a real domain was given) ────────────────
if [[ "$DOMAIN" != "localhost" && ! "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "[7/8] Requesting Let's Encrypt SSL certificate for $DOMAIN..."
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
        --email admin@"$DOMAIN" --redirect || \
    echo "  WARNING: certbot failed — check DNS is pointing to this server."
else
    echo "[7/8] Skipping SSL (no domain provided — using HTTP only)"
fi

# ── 8. Upload reminder ────────────────────────────────────────
echo ""
echo "[8/8] Setup complete!"
echo ""
echo "================================================"
echo "  NEXT STEP: Upload your app files"
echo "================================================"
echo ""
echo "  From your local machine, run:"
echo ""
echo "    scp front.py Tbotx.py Tbot_trend.py license.db ubuntu@$DOMAIN:/app/"
echo "    scp -r templates ubuntu@$DOMAIN:/app/"
echo ""
echo "  Then start the app:"
echo ""
echo "    sudo systemctl start tbot"
echo "    sudo systemctl status tbot"
echo ""

if [[ "$DOMAIN" != "localhost" && ! "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "  Your app will be live at: https://$DOMAIN"
else
    echo "  Your app will be live at: http://$DOMAIN"
fi

echo ""
echo "  Useful commands:"
echo "    sudo systemctl status tbot       # check if running"
echo "    sudo journalctl -u tbot -f       # live logs"
echo "    sudo systemctl restart tbot      # restart after code changes"
echo ""
echo "  IMPORTANT — Oracle Cloud only:"
echo "    Also open ports 80 and 443 in your VCN Security List:"
echo "    OCI Console → Networking → VCN → Security Lists → Add Ingress Rules"
echo "    Source CIDR: 0.0.0.0/0  Protocol: TCP  Ports: 80, 443"
echo ""
