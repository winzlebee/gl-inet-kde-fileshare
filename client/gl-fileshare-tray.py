#!/usr/bin/env python3
"""
GL-FileShare Tray Client
=========================
KDE system-tray application for sending/receiving files via the
GL-FileShare server running on a GL.iNet router.

Requires: PyQt6, requests, dbus-python
"""

import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time

import requests
from PyQt6.QtCore import QTimer, QThread, pyqtSignal, QPointF
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QFont, QPolygonF
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QSystemTrayIcon,
    QVBoxLayout,
)

# ── Configuration ──────────────────────────────────────────────────────────
SERVER_URL = os.environ.get("GL_FILESHARE_SERVER", "http://192.168.1.1:9090")
USERNAME = os.environ.get("GL_FILESHARE_USER", os.environ.get("USER", "unknown"))
HOSTNAME = os.environ.get("GL_FILESHARE_HOST", platform.node() or socket.gethostname())
POLL_INTERVAL = 3  # seconds between checking for new transfers
HEARTBEAT_INTERVAL = 10  # seconds between heartbeats

DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads")


# ── Generate a simple tray icon (no external file needed) ──────────────────
def make_tray_icon():
    """Create a 64x64 tray icon pixmap (file-transfer style)."""
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Circle background
    painter.setBrush(QColor("#2196F3"))
    painter.setPen(QColor("#1976D2"))
    painter.drawEllipse(4, 4, 56, 56)

    # Up arrow (upload)
    painter.setBrush(QColor("white"))
    painter.setPen(QColor("white"))
    painter.drawPolygon(
        QPolygonF([
            QPointF(32, 14),   # top
            QPointF(22, 28),   # bottom-left
            QPointF(42, 28),   # bottom-right
        ])
    )
    painter.drawRect(28, 28, 8, 8)

    # Down arrow (download)
    painter.drawPolygon(
        QPolygonF([
            QPointF(32, 52),   # bottom
            QPointF(22, 38),   # top-left
            QPointF(42, 38),   # top-right
        ])
    )
    painter.drawRect(28, 32, 8, 6)

    painter.end()
    return QIcon(pix)


def make_attention_icon():
    """Create an orange attention icon (same style, different color)."""
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#FF9800"))
    painter.setPen(QColor("#F57C00"))
    painter.drawEllipse(4, 4, 56, 56)

    # Up arrow
    painter.setBrush(QColor("white"))
    painter.setPen(QColor("white"))
    painter.drawPolygon(
        QPolygonF([
            QPointF(32, 14),
            QPointF(22, 28),
            QPointF(42, 28),
        ])
    )
    painter.drawRect(28, 28, 8, 8)

    # Down arrow
    painter.drawPolygon(
        QPolygonF([
            QPointF(32, 52),
            QPointF(22, 38),
            QPointF(42, 38),
        ])
    )
    painter.drawRect(28, 32, 8, 6)

    painter.end()
    return QIcon(pix)


# ── D-Bus notification helper ──────────────────────────────────────────────
def send_kde_notification(title: str, body: str, actions: list[str] | None = None,
                          timeout: int = 0):
    """
    Send a desktop notification via D-Bus.
    actions: list of ("action_id", "label") pairs
    Returns notification ID if successful.
    """
    try:
        import dbus
        bus = dbus.SessionBus()
        notify_obj = bus.get_object(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
        )
        notify_iface = dbus.Interface(notify_obj, "org.freedesktop.Notifications")

        app_name = "GL-FileShare"
        replaces_id = 0
        app_icon = "folder-remote"

        hints = {"urgency": dbus.Byte(1)}  # normal urgency

        action_list = []
        if actions:
            for i, (aid, label) in enumerate(actions):
                action_list.append(aid)
                action_list.append(label)

        return notify_iface.Notify(
            app_name,
            replaces_id,
            app_icon,
            title,
            body,
            action_list,
            hints,
            timeout,
        )
    except Exception:
        # Fallback to notify-send
        try:
            cmd = ["notify-send", title, body]
            if timeout:
                cmd += ["-t", str(timeout)]
            subprocess.run(cmd, capture_output=True)
        except Exception:
            pass
        return 0


# ── Server communication thread ────────────────────────────────────────────
class ServerPoller(QThread):
    """Background thread that polls the server for pending transfers."""

    pending_transfer = pyqtSignal(dict)   # emitted when a new pending transfer is found
    connection_error = pyqtSignal(str)    # emitted on connection failure
    server_status = pyqtSignal(str)       # status updates

    def __init__(self, server_url, username, hostname):
        super().__init__()
        self.server_url = server_url
        self.username = username
        self.hostname = hostname
        self._running = True
        self._known_transfers: set[str] = set()
        self._last_heartbeat = 0

    def run(self):
        # Register first
        self._register()
        time.sleep(0.5)

        while self._running:
            try:
                # Heartbeat
                now = time.time()
                if now - self._last_heartbeat >= HEARTBEAT_INTERVAL:
                    self._heartbeat()
                    self._last_heartbeat = now

                # Check for pending transfers
                self._check_pending()
                self._check_sent()

            except requests.exceptions.ConnectionError:
                self.connection_error.emit(f"Cannot connect to {self.server_url}")
            except Exception as e:
                self.connection_error.emit(str(e))

            # Sleep in small increments so we can exit cleanly
            for _ in range(POLL_INTERVAL * 2):
                if not self._running:
                    return
                time.sleep(0.5)

    def stop(self):
        self._running = False

    def _register(self):
        """Register this client with the server."""
        try:
            resp = requests.post(
                f"{self.server_url}/api/register",
                json={"username": self.username, "hostname": self.hostname},
                timeout=5,
            )
            if resp.ok:
                self.server_status.emit(f"Connected to {self.server_url}")
            else:
                self.server_status.emit(f"Registration failed: {resp.text}")
        except Exception as e:
            self.server_status.emit(f"Registration error: {e}")

    def _heartbeat(self):
        try:
            requests.post(
                f"{self.server_url}/api/heartbeat",
                json={"username": self.username},
                timeout=3,
            )
        except Exception:
            pass

    def _check_pending(self):
        """Check for transfers addressed to us."""
        try:
            resp = requests.get(
                f"{self.server_url}/api/pending/{self.username}",
                timeout=5,
            )
            if not resp.ok:
                return
            data = resp.json()
            for t in data.get("transfers", []):
                tid = t["transfer_id"]
                if tid not in self._known_transfers and t["status"] == "pending":
                    self._known_transfers.add(tid)
                    self.pending_transfer.emit(t)
        except Exception:
            pass

    def _check_sent(self):
        """Check the status of transfers we sent (for notification of acceptance/rejection)."""
        # This is handled by the pending_transfer signal for incoming only.
        # Sent transfers are checked elsewhere.
        pass


# ── Transfer response dialog ───────────────────────────────────────────────
class TransferDialog(QDialog):
    """Dialog shown when someone wants to send you a file."""

    def __init__(self, transfer_info: dict, server_url: str, download_dir: str, parent=None):
        super().__init__(parent)
        self.transfer_info = transfer_info
        self.server_url = server_url
        self.download_dir = download_dir
        self.result_action = "reject"

        self.setWindowTitle("Incoming File Transfer")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        from_user = transfer_info.get("from_user", "Unknown")
        filename = transfer_info.get("filename", "unknown")
        file_size = transfer_info.get("file_size", 0)
        size_str = self._format_size(file_size)

        label = QLabel(
            f"<b>{from_user}</b> wants to send you a file:\n\n"
            f"<b>{filename}</b>  ({size_str})"
        )
        label.setWordWrap(True)
        layout.addWidget(label)

        buttons = QDialogButtonBox()
        btn_accept = buttons.addButton("Accept", QDialogButtonBox.ButtonRole.AcceptRole)
        btn_reject = buttons.addButton("Reject", QDialogButtonBox.ButtonRole.RejectRole)
        btn_accept.setStyleSheet("background-color: #4CAF50; color: white; padding: 6px 16px;")
        btn_reject.setStyleSheet("background-color: #f44336; color: white; padding: 6px 16px;")
        layout.addWidget(buttons)

        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self._on_reject)

    def _format_size(self, size_bytes):
        for unit in ("B", "KB", "MB", "GB"):
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"

    def _on_accept(self):
        self.result_action = "accept"
        self.accept()

    def _on_reject(self):
        self.result_action = "reject"
        self.reject()


# ── Send File dialog (recipient picker) ────────────────────────────────────
class RecipientPicker(QDialog):
    """Dialog to pick a recipient from online clients."""

    def __init__(self, server_url: str, parent=None):
        super().__init__(parent)
        self.server_url = server_url
        self.selected_user = None

        self.setWindowTitle("Send File - Select Recipient")
        self.setMinimumSize(300, 250)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Online clients:"))

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        refresh_btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel)
        refresh_btn.button(QDialogButtonBox.StandardButton.Ok).setText("Send")
        layout.addWidget(refresh_btn)

        refresh_btn.accepted.connect(self._on_accept)
        refresh_btn.rejected.connect(self.reject)

        self._refresh_clients()

    def _refresh_clients(self):
        self.list_widget.clear()
        try:
            resp = requests.get(f"{self.server_url}/api/clients", timeout=5)
            if resp.ok:
                data = resp.json()
                for c in data.get("clients", []):
                    if c["username"] != USERNAME:
                        item = QListWidgetItem(f"{c['username']}  ({c.get('hostname', '?')})")
                        item.setData(1, c["username"])  # Qt.UserRole = 1
                        self.list_widget.addItem(item)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not fetch client list:\n{e}")

        if self.list_widget.count() == 0:
            item = QListWidgetItem("No other clients online")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.list_widget.addItem(item)

    def _on_accept(self):
        current = self.list_widget.currentItem()
        if current and current.data(1):
            self.selected_user = current.data(1)
            self.accept()
        else:
            QMessageBox.information(self, "No Selection", "Please select a recipient.")


# ── Main Tray Application ──────────────────────────────────────────────────
class FileShareTray:
    """Main system-tray application."""

    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        # State
        self.server_url = SERVER_URL
        self.username = USERNAME
        self.hostname = HOSTNAME
        self.download_dir = DOWNLOAD_DIR

        # Icons
        self.icon_normal = make_tray_icon()
        self.icon_attention = make_attention_icon()

        # Tray
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(self.icon_normal)
        self.tray.setToolTip("GL-FileShare - File sharing via router")

        # Menu
        self.menu = QMenu()
        self._build_menu()
        self.tray.setContextMenu(self.menu)

        self.tray.show()

        # Server poller
        self.poller = ServerPoller(self.server_url, self.username, self.hostname)
        self.poller.pending_transfer.connect(self._on_pending_transfer)
        self.poller.connection_error.connect(self._on_connection_error)
        self.poller.server_status.connect(self._on_status)
        self.poller.start()

        # Check sent transfers periodically
        self.sent_timer = QTimer()
        self.sent_timer.timeout.connect(self._check_sent_transfers)
        self.sent_timer.start(5000)  # every 5s

        # Track which transfers we've already seen the result for
        self._sent_transfer_states: dict[str, str] = {}

        print(f"GL-FileShare tray started. Server: {self.server_url}")
        print(f"User: {self.username}  Host: {self.hostname}")

    def _build_menu(self):
        self.menu.clear()

        action_send = QAction("Send File...", self.menu)
        action_send.triggered.connect(self._send_file)
        self.menu.addAction(action_send)

        self.menu.addSeparator()

        action_check = QAction("Check for Files", self.menu)
        action_check.triggered.connect(self._check_now)
        self.menu.addAction(action_check)

        action_status = QAction("Status", self.menu)
        action_status.triggered.connect(self._show_status)
        self.menu.addAction(action_status)

        self.menu.addSeparator()

        action_quit = QAction("Quit", self.menu)
        action_quit.triggered.connect(self._quit)
        self.menu.addAction(action_quit)

    def _send_file(self):
        """Send a file flow: pick file → pick recipient → upload."""
        file_path, _ = QFileDialog.getOpenFileName(
            None, "Select File to Send", os.path.expanduser("~")
        )
        if not file_path:
            return

        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        # Pick recipient
        picker = RecipientPicker(self.server_url)
        if picker.exec() != QDialog.DialogCode.Accepted or not picker.selected_user:
            return

        to_user = picker.selected_user

        # Step 1: Create transfer request
        try:
            resp = requests.post(
                f"{self.server_url}/api/send-request",
                json={
                    "from_user": self.username,
                    "to_user": to_user,
                    "filename": file_name,
                    "file_size": file_size,
                },
                timeout=10,
            )
            if not resp.ok:
                QMessageBox.critical(None, "Error", f"Server error: {resp.text}")
                return
            transfer_id = resp.json()["transfer_id"]
        except Exception as e:
            QMessageBox.critical(None, "Error", f"Failed to create transfer:\n{e}")
            return

        # Step 2: Upload file
        progress = QProgressDialog(f"Uploading {file_name}...", "Cancel", 0, 100)
        progress.setWindowTitle("Sending File")
        progress.setWindowModality(Qt.WindowModality.NonModal)
        progress.show()

        try:
            with open(file_path, "rb") as f:
                file_data = f.read()

            progress.setValue(50)
            QApplication.processEvents()

            resp = requests.post(
                f"{self.server_url}/api/upload/{transfer_id}",
                data=file_data,
                headers={"Content-Type": "application/octet-stream"},
                timeout=60,
            )
            progress.setValue(100)

            if resp.ok:
                progress.close()
                send_kde_notification(
                    "File Sent",
                    f"'{file_name}' sent to {to_user}.\nWaiting for them to accept...",
                )
                QMessageBox.information(
                    None,
                    "File Sent",
                    f"'{file_name}' sent to {to_user}.\nThey will be notified and can accept or reject it.",
                )
            else:
                progress.close()
                QMessageBox.critical(None, "Error", f"Upload failed: {resp.text}")

        except Exception as e:
            progress.close()
            QMessageBox.critical(None, "Error", f"Upload failed:\n{e}")

    def _on_pending_transfer(self, transfer_info: dict):
        """Handle an incoming transfer request."""
        from_user = transfer_info.get("from_user", "Unknown")
        filename = transfer_info.get("filename", "unknown")
        file_size = transfer_info.get("file_size", 0)
        transfer_id = transfer_info.get("transfer_id", "")
        size_mb = file_size / (1024 * 1024)

        # Blinking attention tray icon
        self.tray.setIcon(self.icon_attention)
        self.tray.setToolTip(f"Incoming file from {from_user}!")

        # Show notification
        send_kde_notification(
            "Incoming File Transfer",
            f"{from_user} wants to send you:\n{filename} ({size_mb:.1f} MB)",
            actions=[("accept", "Accept"), ("reject", "Reject")],
            timeout=30000,
        )

        # Show dialog
        dialog = TransferDialog(transfer_info, self.server_url, self.download_dir)
        result = dialog.exec()

        if result == QDialog.DialogCode.Accepted:
            self._accept_transfer(transfer_id, filename)
        else:
            self._reject_transfer(transfer_id, from_user, filename)

        self.tray.setIcon(self.icon_normal)
        self.tray.setToolTip("GL-FileShare - File sharing via router")

    def _accept_transfer(self, transfer_id: str, filename: str):
        """Accept a transfer and download the file."""
        # Step 1: Respond "accept"
        try:
            resp = requests.post(
                f"{self.server_url}/api/respond/{transfer_id}",
                json={"action": "accept"},
                timeout=5,
            )
            if not resp.ok:
                QMessageBox.critical(None, "Error", f"Failed to accept: {resp.text}")
                return
        except Exception as e:
            QMessageBox.critical(None, "Error", f"Failed to respond:\n{e}")
            return

        # Step 2: Choose download location
        save_path, _ = QFileDialog.getSaveFileName(
            None, "Save File As", os.path.join(self.download_dir, filename)
        )
        if not save_path:
            # User cancelled after accepting — still download to Downloads
            save_path = os.path.join(self.download_dir, filename)

        # Step 3: Download file
        progress = QProgressDialog(f"Downloading {filename}...", "Cancel", 0, 0)
        progress.setWindowTitle("Receiving File")
        progress.setWindowModality(Qt.WindowModality.NonModal)
        progress.show()

        try:
            resp = requests.get(
                f"{self.server_url}/api/download/{transfer_id}",
                stream=True,
                timeout=120,
            )
            if resp.status_code == 200:
                total = int(resp.headers.get("Content-Length", 0))
                progress.setMaximum(100)
                downloaded = 0
                with open(save_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            progress.setValue(int(downloaded / total * 100))
                        QApplication.processEvents()

                progress.close()
                send_kde_notification(
                    "File Received",
                    f"'{filename}' saved to Downloads.",
                    timeout=5000,
                )
                QMessageBox.information(
                    None, "Download Complete",
                    f"File saved to:\n{save_path}"
                )
            else:
                progress.close()
                QMessageBox.critical(None, "Error", f"Download failed: HTTP {resp.status_code}")

        except Exception as e:
            progress.close()
            QMessageBox.critical(None, "Error", f"Download failed:\n{e}")

        # Clean up transfer on server
        try:
            requests.delete(f"{self.server_url}/api/transfer/{transfer_id}", timeout=5)
        except Exception:
            pass

    def _reject_transfer(self, transfer_id: str, from_user: str, filename: str):
        """Reject an incoming transfer."""
        try:
            requests.post(
                f"{self.server_url}/api/respond/{transfer_id}",
                json={"action": "reject"},
                timeout=5,
            )
        except Exception:
            pass

    def _check_sent_transfers(self):
        """Poll status of transfers we initiated (to know if accepted/rejected)."""
        try:
            resp = requests.get(
                f"{self.server_url}/api/my-requests/{self.username}",
                timeout=5,
            )
            if not resp.ok:
                return
            data = resp.json()
            for t in data.get("transfers", []):
                tid = t["transfer_id"]
                status = t["status"]
                prev = self._sent_transfer_states.get(tid)
                if prev != status:
                    self._sent_transfer_states[tid] = status
                    if status == "accepted":
                        send_kde_notification(
                            "File Transfer Accepted",
                            f"{t['to_user']} accepted '{t['filename']}'.",
                            timeout=5000,
                        )
                        # Clean up
                        try:
                            requests.delete(f"{self.server_url}/api/transfer/{tid}", timeout=5)
                        except Exception:
                            pass
                    elif status == "rejected":
                        send_kde_notification(
                            "File Transfer Rejected",
                            f"{t['to_user']} rejected '{t['filename']}'.",
                            timeout=5000,
                        )
                        try:
                            requests.delete(f"{self.server_url}/api/transfer/{tid}", timeout=5)
                        except Exception:
                            pass
        except Exception:
            pass

    def _check_now(self):
        """Force an immediate check for pending transfers."""
        try:
            resp = requests.get(
                f"{self.server_url}/api/pending/{self.username}",
                timeout=5,
            )
            if not resp.ok:
                QMessageBox.information(None, "Check", "Server unreachable.")
                return
            data = resp.json()
            transfers = data.get("transfers", [])
            if not transfers:
                QMessageBox.information(None, "No Files", "No pending file transfers.")
            else:
                # Show oldest pending
                for t in transfers:
                    if t["status"] == "pending":
                        self._on_pending_transfer(t)
                        return
                QMessageBox.information(None, "No Pending", "No pending transfers right now.")
        except Exception as e:
            QMessageBox.warning(None, "Error", f"Could not check:\n{e}")

    def _show_status(self):
        """Show connection status."""
        try:
            resp = requests.get(f"{self.server_url}/api/clients", timeout=5)
            if resp.ok:
                data = resp.json()
                client_count = len(data.get("clients", []))
                QMessageBox.information(
                    None, "Status",
                    f"Server: {self.server_url}\n"
                    f"Connected as: {self.username}\n"
                    f"Online clients: {client_count}"
                )
            else:
                QMessageBox.warning(
                    None, "Status",
                    f"Server: {self.server_url}\nStatus: Error ({resp.status_code})"
                )
        except Exception as e:
            QMessageBox.warning(
                None, "Status",
                f"Server: {self.server_url}\nStatus: Unreachable\n{e}"
            )

    def _on_connection_error(self, msg: str):
        """Handle connection errors."""
        self.tray.setToolTip(f"GL-FileShare - Disconnected: {msg}")

    def _on_status(self, msg: str):
        """Handle status updates."""
        print(f"[Status] {msg}")

    def _quit(self):
        """Clean shutdown."""
        self.poller.stop()
        self.sent_timer.stop()
        self.poller.wait(2000)
        self.tray.hide()
        self.app.quit()

    def run(self):
        return self.app.exec()


# ── Entry point ────────────────────────────────────────────────────────────
def main():
    tray = FileShareTray()
    sys.exit(tray.run())


if __name__ == "__main__":
    main()
