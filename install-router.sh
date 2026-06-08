#!/bin/sh
# =============================================================================
# GL-FileShare Router Management Script
# =============================================================================
# Run this FROM YOUR LOCAL MACHINE (not on the router).
# It uses scp + ssh to manage the GL-FileShare service on a GL.iNet router.
#
# Usage:
#   ./install-router.sh install            Fresh install
#   ./install-router.sh install --update   Update existing installation
#   ./install-router.sh uninstall           Remove GL-FileShare from router
#   ./install-router.sh attach              SSH in, stop service, run manually
#   ./install-router.sh status              Show service status
#   ./install-router.sh logs                Show recent GL-FileShare logs
#   ./install-router.sh restart             Restart the service
#   ./install-router.sh stop                Stop the service
#   ./install-router.sh start               Start the service
#
# =============================================================================

set -e

ROUTER_IP="192.168.1.1"
CONTROL_PATH="/tmp/gl-fileshare-ssh-socket-$$"
SSH_OPTS="-o ControlMaster=auto -o ControlPath=${CONTROL_PATH} -o ControlPersist=60"
ROUTER_SSH="ssh ${SSH_OPTS} root@${ROUTER_IP}"
ROUTER_SCP="scp -O ${SSH_OPTS}"
INSTALL_DIR="/usr/share/gl-fileshare"
INIT_SCRIPT="/etc/init.d/gl-fileshare"
SERVER_SCRIPT="server/gl-fileshare-server.py"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Colors ──────────────────────────────────────────────────────────────────
BOLD='\033[1m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
RESET='\033[0m'

# Simple printf wrapper to avoid repeating %b\n everywhere
_print() { printf '%b\n' "$@"; }

# ── Banner ──────────────────────────────────────────────────────────────────
banner() {
    _print ""
    _print "${BOLD}═════════════════════════════════════════${RESET}"
    _print "${BOLD}  GL-FileShare Router Manager${RESET}"
    _print "${BOLD}═════════════════════════════════════════${RESET}"
    _print ""
}

# ── SSH helpers ─────────────────────────────────────────────────────────────
ssh_cmd() {
    $ROUTER_SSH "$@"
}

ssh_quiet() {
    ssh ${SSH_OPTS} -q -o ConnectTimeout=5 root@"$ROUTER_IP" "$@" 2>/dev/null
}

# Open a master connection so the user only types their password once
open_master() {
    ssh ${SSH_OPTS} -M -f -N root@"$ROUTER_IP" 2>/dev/null && return 0 || return 1
}

# Clean up the master socket on exit
cleanup() { ssh -q -S "$CONTROL_PATH" -O exit root@"$ROUTER_IP" 2>/dev/null; }
trap cleanup EXIT

# Check we can reach the router
check_router() {
    # Open multiplexed master connection (user enters password here, once)
    if ! open_master; then
        _print "${RED}ERROR: Cannot connect to router at ${ROUTER_IP}${RESET}"
        echo "Make sure SSH is enabled on the router and it's reachable."
        exit 1
    fi
}

# Check if GL-FileShare is already installed on the router
is_installed() {
    ssh_quiet "[ -f ${INIT_SCRIPT} ]" && return 0 || return 1
}

# Check if the service is currently running
is_running() {
    ssh_quiet "/etc/init.d/gl-fileshare status 2>/dev/null | grep -q 'running'" && return 0 || return 1
}

# ── Commands ────────────────────────────────────────────────────────────────

cmd_install() {
    UPDATE_FLAG=""
    if [ "${1:-}" = "--update" ]; then
        UPDATE_FLAG="1"
    fi

    banner
    check_router

    if is_installed && [ -z "$UPDATE_FLAG" ]; then
        _print "${YELLOW}GL-FileShare is already installed on the router.${RESET}"
        echo ""
        echo "To update the existing installation, use:"
        echo "  ${BOLD}./install-router.sh install --update${RESET}"
        echo ""
        echo "It will re-copy files and restart the service."
        exit 0
    fi

    if [ -n "$UPDATE_FLAG" ]; then
        _print "${BOLD}Updating existing GL-FileShare installation...${RESET}"
    else
        _print "${BOLD}Installing GL-FileShare on the router...${RESET}"
    fi
    echo ""

    # ── Step 1: Copy server script to router ──────────────────────────────
    echo "[1/5] Copying server script to router..."
    $ROUTER_SCP "${SCRIPT_DIR}/${SERVER_SCRIPT}" root@"${ROUTER_IP}":/tmp/gl-fileshare-server.py
    _print "  ${GREEN}Done.${RESET}"

    # ── Step 2: Install Python 3 if needed ────────────────────────────────
    echo ""
    echo "[2/5] Checking Python 3 on router..."
    $ROUTER_SSH << 'EOF'
if ! command -v python3 >/dev/null 2>&1; then
    echo "  Installing python3-light (this may take a minute)..."
    opkg update
    opkg install python3-light python3-base
    echo "  Python 3 installed."
else
    echo "  Python 3 already installed: $(python3 --version)"
fi
EOF

    # ── Step 3: Create directories and copy files ─────────────────────────
    echo ""
    echo "[3/5] Installing server files..."
    $ROUTER_SSH << EOF
mkdir -p ${INSTALL_DIR}
cp /tmp/gl-fileshare-server.py ${INSTALL_DIR}/gl-fileshare-server.py
chmod +x ${INSTALL_DIR}/gl-fileshare-server.py
rm -f /tmp/gl-fileshare-server.py
echo "  Server installed to ${INSTALL_DIR}/gl-fileshare-server.py"
EOF

    # ── Step 4: Create init.d service ─────────────────────────────────────
    echo ""
    echo "[4/5] Setting up init.d service..."

    # Determine storage dir
    STORE_DIR=$(ssh_quiet "[ -d /mnt/sda1 ] && echo '/mnt/sda1/gl-fileshare/downloads' || echo '/tmp/filestore'")

    $ROUTER_SSH << EOF
cat > ${INIT_SCRIPT} << 'INITEOF'
#!/bin/sh /etc/rc.common
# GL-FileShare init script for OpenWrt (procd)

START=95
STOP=10
USE_PROCD=1

PROG="${INSTALL_DIR}/gl-fileshare-server.py"
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
chmod +x ${INIT_SCRIPT}
echo "  Init script created at ${INIT_SCRIPT}"
EOF

    # ── Step 5: Enable and start the service ──────────────────────────────
    echo ""
    echo "[5/5] Starting service..."

    if [ -n "$UPDATE_FLAG" ]; then
        $ROUTER_SSH << 'EOF'
if /etc/init.d/gl-fileshare status 2>/dev/null | grep -q running; then
    /etc/init.d/gl-fileshare restart 2>/dev/null || {
        echo "  procd restart failed; killing and starting directly..."
        kill $(cat /var/run/gl-fileshare.pid) 2>/dev/null
        sleep 1
        /usr/bin/python3 /usr/share/gl-fileshare/gl-fileshare-server.py &
        echo $! > /var/run/gl-fileshare.pid
    }
else
    /etc/init.d/gl-fileshare start 2>/dev/null || {
        echo "  procd start failed; launching directly..."
        /usr/bin/python3 /usr/share/gl-fileshare/gl-fileshare-server.py &
        echo $! > /var/run/gl-fileshare.pid
    }
fi
EOF
        echo ""
        _print "${GREEN}${BOLD} ✓ Update complete — service restarted${RESET}"
    else
        $ROUTER_SSH << 'EOF'
/etc/init.d/gl-fileshare enable
/etc/init.d/gl-fileshare start 2>/dev/null || {
    echo "  procd start failed; launching directly..."
    /usr/bin/python3 /usr/share/gl-fileshare/gl-fileshare-server.py &
    echo $! > /var/run/gl-fileshare.pid
}
EOF
        echo ""
        _print "${GREEN}${BOLD} ✓ Installation complete${RESET}"
    fi

    echo ""
    echo "  Server URL: http://${ROUTER_IP}:9090"
    echo "  Storage:    ${STORE_DIR}"
    echo ""
    echo "  Manage the service:"
    echo "    /etc/init.d/gl-fileshare start|stop|restart|enable|disable"
    echo "    logread | grep gl-fileshare    (view logs)"
    echo "========================================="


}

cmd_uninstall() {
    banner
    check_router

    if ! is_installed; then
        _print "${YELLOW}GL-FileShare is not installed on the router.${RESET}"
        exit 0
    fi

    _print "${BOLD}Uninstalling GL-FileShare from router...${RESET}"
    echo ""

    $ROUTER_SSH << 'EOF'
echo "  Stopping service..."
/etc/init.d/gl-fileshare stop 2>/dev/null || {
    kill $(cat /var/run/gl-fileshare.pid) 2>/dev/null
    rm -f /var/run/gl-fileshare.pid
}

echo "  Disabling auto-start..."
/etc/init.d/gl-fileshare disable 2>/dev/null || true

echo "  Removing init script..."
rm -f /etc/init.d/gl-fileshare

echo "  Removing server files..."
rm -rf /usr/share/gl-fileshare

echo "  Removing PID file..."
rm -f /var/run/gl-fileshare.pid

EOF

    echo ""
    _print "${GREEN}${BOLD} ✓ GL-FileShare has been uninstalled from the router.${RESET}"
    echo ""
    echo "  Note: Downloaded files in /tmp/filestore/ or /mnt/sda1/gl-fileshare/"
    echo "  were not deleted. Remove them manually if desired."
}

cmd_attach() {
    banner
    check_router

    _print "${BOLD}Attaching to GL-FileShare server...${RESET}"
    echo "  (Press Ctrl+C to exit)"
    echo ""

    if is_running; then
        echo "  Stopping background service first..."
        $ROUTER_SSH '/etc/init.d/gl-fileshare stop 2>/dev/null; kill $(cat /var/run/gl-fileshare.pid) 2>/dev/null; rm -f /var/run/gl-fileshare.pid; true'
        sleep 1
    fi

    # Run the server interactively so you see console output locally
    $ROUTER_SSH -t "python3 ${INSTALL_DIR}/gl-fileshare-server.py"
}

cmd_status() {
    banner
    check_router
    $ROUTER_SSH << 'EOF'
if [ -f /etc/init.d/gl-fileshare ]; then
    /etc/init.d/gl-fileshare status 2>&1 || true
else
    echo "GL-FileShare is not installed."
fi
EOF
}

cmd_logs() {
    banner
    check_router
    $ROUTER_SSH 'logread | grep gl-fileshare | tail -30'
}

cmd_restart() {
    banner
    check_router
    echo "Restarting GL-FileShare..."
    $ROUTER_SSH '/etc/init.d/gl-fileshare restart 2>/dev/null || echo "Restart failed — service may not be running."'
    _print "${GREEN}Done.${RESET}"
}

cmd_stop() {
    banner
    check_router
    echo "Stopping GL-FileShare..."
    $ROUTER_SSH '/etc/init.d/gl-fileshare stop 2>/dev/null || echo "Stop failed — service may not be running."'
    _print "${GREEN}Done.${RESET}"
}

cmd_start() {
    banner
    check_router
    echo "Starting GL-FileShare..."
    $ROUTER_SSH '/etc/init.d/gl-fileshare start 2>/dev/null || echo "Start failed — service may already be running."'
    _print "${GREEN}Done.${RESET}"
}

# ── Help ────────────────────────────────────────────────────────────────────
cmd_help() {
    cat << EOF

${BOLD}GL-FileShare Router Manager${RESET}

Run from your local machine to manage the GL-FileShare service on the router.

${BOLD}Usage:${RESET}
  ./install-router.sh <command> [options]

${BOLD}Commands:${RESET}
  install             Fresh install — copies files and starts the service
  install --update    Update an existing installation and restart the service
  uninstall           Remove GL-FileShare from the router
  attach              SSH in, kill the background service, run interactively
  status              Show the current service status
  logs                Show the last 30 log entries from the router
  restart             Restart the service
  stop                Stop the service
  start               Start the service

${BOLD}Examples:${RESET}
  # First-time setup
  ./install-router.sh install

  # Push an updated server script to the router
  ./install-router.sh install --update

  # Debug: run the server interactively to see output
  ./install-router.sh attach

  # Remove everything from the router
  ./install-router.sh uninstall

EOF
}

# ── Main ────────────────────────────────────────────────────────────────────
if [ $# -eq 0 ]; then
    cmd_help
    exit 1
fi

COMMAND="$1"
shift

case "$COMMAND" in
    install)  cmd_install "$@" ;;
    uninstall) cmd_uninstall ;;
    attach|debug) cmd_attach ;;
    status)   cmd_status ;;
    logs)     cmd_logs ;;
    restart)  cmd_restart ;;
    stop)     cmd_stop ;;
    start)    cmd_start ;;
    help|--help|-h) cmd_help ;;
    *)
        _print "${RED}Unknown command: ${COMMAND}${RESET}"
        echo ""
        cmd_help
        exit 1
        ;;
esac
