#!/bin/sh
# =============================================================================
# GL-FileShare Router Installation Script
# =============================================================================
# Run this ON the GL.iNet router (Flint 2 / OpenWrt).
#
# Usage:
#   1. Copy files to the router:
#      scp install-router.sh server/gl-fileshare-server.py root@192.168.1.1:/tmp/
#
#   2. SSH in and run:
#      ssh root@192.168.1.1
#      sh /tmp/install-router.sh
#
# =============================================================================

set -e

INSTALL_DIR="/usr/share/gl-fileshare"
INIT_SCRIPT="/etc/init.d/gl-fileshare"
DISK_STORE_DIR="/mnt/sda1/gl-fileshare"
TMP_STORE_DIR="/tmp/filestore"

echo "====================================="
echo " GL-FileShare Router Installer"
echo "====================================="

# ── 1. Detect storage ───────────────────────────────────────────────────
echo ""
echo "[1/5] Checking storage..."

if [ -d "/mnt/sda1" ]; then
    STORE_DIR="$DISK_STORE_DIR"
    echo "  Attached disk found at /mnt/sda1"
    echo "  Files will be stored in: $STORE_DIR"
else
    STORE_DIR="$TMP_STORE_DIR"
    echo "  No attached disk. Using RAM storage: $STORE_DIR"
    echo "  NOTE: files are lost on reboot and limited to ~400 MB."
fi
mkdir -p "$STORE_DIR"

# ── 2. Install Python 3 ────────────────────────────────────────────────
echo ""
echo "[2/5] Checking Python 3..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "  Installing python3-light (this may take a minute)..."
    opkg update
    opkg install python3-light python3-base
    echo "  Python 3 installed."
else
    echo "  Python 3 already installed: $(python3 --version)"
fi

# ── 3. Create install directory ─────────────────────────────────────────
echo ""
echo "[3/5] Creating install directory..."
mkdir -p "$INSTALL_DIR"

# ── 4. Copy server script ───────────────────────────────────────────────
echo ""
echo "[4/5] Installing server script..."
if [ -f /tmp/gl-fileshare-server.py ]; then
    cp /tmp/gl-fileshare-server.py "$INSTALL_DIR/gl-fileshare-server.py"
elif [ -f ./gl-fileshare-server.py ]; then
    cp ./gl-fileshare-server.py "$INSTALL_DIR/gl-fileshare-server.py"
else
    echo "  ERROR: gl-fileshare-server.py not found!"
    echo "  Copy it to /tmp/ on the router first:"
    echo "    scp server/gl-fileshare-server.py root@192.168.1.1:/tmp/"
    exit 1
fi
chmod +x "$INSTALL_DIR/gl-fileshare-server.py"
echo "  Server installed to $INSTALL_DIR/gl-fileshare-server.py"

# ── 5. Create init.d service ────────────────────────────────────────────
echo ""
echo "[5/5] Creating init.d service..."

cat > "$INIT_SCRIPT" << INITEOF
#!/bin/sh /etc/rc.common
# GL-FileShare init script for OpenWrt (procd)

START=95
STOP=10
USE_PROCD=1

PROG="$INSTALL_DIR/gl-fileshare-server.py"

PIDFILE="/var/run/gl-fileshare.pid"

start_service() {
    echo "Starting GL-FileShare server..."
    procd_open_instance
    procd_set_param command /usr/bin/python3 "\$PROG"
    procd_set_param pidfile "\$PIDFILE"
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_set_param respawn 3600 5 15
    procd_close_instance
    echo "GL-FileShare started on port 9090"
    echo "Storage: $STORE_DIR"
}

stop_service() {
    echo "Stopping GL-FileShare server..."
    if [ -f "\$PIDFILE" ]; then
        kill "\$(cat "\$PIDFILE")" 2>/dev/null
        rm -f "\$PIDFILE"
    fi
}

service_triggers() {
    procd_add_reload_trigger "gl-fileshare"
}

reload_service() {
    stop
    start
}
INITEOF

chmod +x "$INIT_SCRIPT"

# ── Enable and start the service ────────────────────────────────────────
echo ""
/etc/init.d/gl-fileshare enable
/etc/init.d/gl-fileshare start 2>/dev/null || {
    echo "  procd start failed; launching directly..."
    /usr/bin/python3 "$INSTALL_DIR/gl-fileshare-server.py" &
    echo $! > /var/run/gl-fileshare.pid
    echo "  Started (PID $!)"
}

# ── Firewall note ───────────────────────────────────────────────────────
echo ""
echo "====================================="
echo " ✓ Installation Complete"
echo "====================================="
echo ""
echo " Server: http://192.168.1.1:9090"
echo " Storage: $STORE_DIR"
echo ""
echo " If the firewall blocks port 9090, open it with:"
echo "   iptables -I INPUT -p tcp --dport 9090 -j ACCEPT"
echo ""
echo " Manage the service:"
echo "   /etc/init.d/gl-fileshare start|stop|restart|enable|disable"
echo "   logread | grep gl-fileshare    (view logs)"
echo "====================================="
