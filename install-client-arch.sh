#!/bin/sh
# =============================================================================
# GL-FileShare Client Installer (Arch Linux / KDE)
# =============================================================================
# Installs the GL-FileShare tray application and its dependencies on
# Arch Linux with KDE Plasma.
#
# Usage:
#   chmod +x install-client-arch.sh
#   ./install-client-arch.sh
#
# =============================================================================

set -e

INSTALL_DIR="/opt/gl-fileshare/client"
AUTOSTART_DIR="${HOME}/.config/autostart"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "====================================="
echo " GL-FileShare Client Installer"
echo "  Arch Linux / KDE Plasma"
echo "====================================="

# ── 1. Install dependencies ─────────────────────────────────────────────
echo ""
echo "[1/4] Installing dependencies via pacman..."
DEPS="python-pyqt6 python-requests python-dbus"
echo "  Packages: $DEPS"
sudo pacman -S --needed --noconfirm $DEPS 2>/dev/null || {
    echo ""
    echo "  ERROR: pacman install failed. Are you on Arch Linux?"
    echo "  You can install dependencies manually:"
    echo "    sudo pacman -S python-pyqt6 python-requests python-dbus"
    exit 1
}
echo "  Dependencies installed."

# ── 2. Copy client script ───────────────────────────────────────────────
echo ""
echo "[2/4] Installing client script..."
sudo mkdir -p "$INSTALL_DIR"

if [ -f "$SCRIPT_DIR/client/gl-fileshare-tray.py" ]; then
    sudo cp "$SCRIPT_DIR/client/gl-fileshare-tray.py" "$INSTALL_DIR/"
elif [ -f "$SCRIPT_DIR/gl-fileshare-tray.py" ]; then
    sudo cp "$SCRIPT_DIR/gl-fileshare-tray.py" "$INSTALL_DIR/"
else
    echo "  ERROR: gl-fileshare-tray.py not found!"
    echo "  Expected at: $SCRIPT_DIR/client/gl-fileshare-tray.py"
    echo "  Run this script from the project root."
    exit 1
fi
sudo chmod +x "$INSTALL_DIR/gl-fileshare-tray.py"
echo "  Installed to $INSTALL_DIR/gl-fileshare-tray.py"

# ── 3. Install desktop autostart entry ──────────────────────────────────
echo ""
echo "[3/4] Installing autostart entry..."
mkdir -p "$AUTOSTART_DIR"

cat > "$AUTOSTART_DIR/gl-fileshare-tray.desktop" << DESKEOF
[Desktop Entry]
Type=Application
Name=GL-FileShare
Comment=File sharing via GL.iNet router
Icon=folder-remote
Exec=/usr/bin/python3 $INSTALL_DIR/gl-fileshare-tray.py
Terminal=false
Categories=Network;FileTransfer;
StartupNotify=false
X-KDE-autostart-phase=2
DESKEOF

echo "  Autostart entry created: $AUTOSTART_DIR/gl-fileshare-tray.desktop"

# ── 4. Launch now ───────────────────────────────────────────────────────
echo ""
echo "[4/4] Launching GL-FileShare tray..."
echo ""

# Check if KDE is running
if [ "$XDG_CURRENT_DESKTOP" = "KDE" ] || pidof plasmashell >/dev/null 2>&1; then
    echo "  Starting GL-FileShare tray now..."
    nohup /usr/bin/python3 "$INSTALL_DIR/gl-fileshare-tray.py" >/dev/null 2>&1 &
    echo "  Tray icon should appear in your system tray momentarily."
else
    echo "  KDE not detected. The tray will launch on next KDE login."
    echo ""
    echo "  To start manually:"
    echo "    python3 $INSTALL_DIR/gl-fileshare-tray.py"
fi

echo ""
echo "====================================="
echo " ✓ Client Installation Complete"
echo "====================================="
echo ""
echo " The tray icon auto-starts on login."
echo ""
echo " Configuration (optional):"
echo "   GL_FILESHARE_SERVER  — router address (default: http://192.168.1.1:9090)"
echo "   GL_FILESHARE_USER    — your display name  (default: \$USER)"
echo ""
echo "   Add these to ~/.config/environment.d/gl-fileshare.conf if needed:"
echo "     GL_FILESHARE_SERVER=http://192.168.1.1:9090"
echo "     GL_FILESHARE_USER=alice"
echo ""
echo " To uninstall:"
echo "   sudo rm -rf $INSTALL_DIR"
echo "   rm ~/.config/autostart/gl-fileshare-tray.desktop"
echo "====================================="
