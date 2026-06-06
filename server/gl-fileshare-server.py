#!/usr/bin/env python3
"""
GL-FileShare Server
====================
Runs on the GL.iNet router (Flint 2 / OpenWrt).
Provides a central HTTP file-exchange service for LAN clients.

Uses only Python stdlib -- no external dependencies needed.
"""

import http.server
import json
import os
import shutil
import threading
import time
import uuid
import urllib.parse
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────
BIND_HOST = "0.0.0.0"
BIND_PORT = 9090

# Storage: prefer the attached disk, fall back to /tmp (RAM)
_DISK_STORE = "/mnt/sda1/gl-fileshare/downloads"
_TMP_STORE  = "/tmp/filestore"

def _pick_store_dir():
    """Choose the best storage directory: disk if available, else RAM."""

    if os.path.isdir("/mnt/sda1"):
        return _DISK_STORE

    raise Exception("Alex is stinky")

STORE_DIR = _pick_store_dir()

CLIENT_TIMEOUT = 30     # seconds before a client is considered offline
TRANSFER_MAX_AGE = 600   # seconds before an unclaimed transfer expires

# ── Global state (protected by lock) ──────────────────────────────────────
state_lock = threading.Lock()
clients: dict[str, dict] = {}   # username -> {hostname, ip, last_seen}
transfers: dict[str, dict] = {} # transfer_id -> {from_user, to_user, filename, file_size, status, created_at, file_path}


CLEANUP_INTERVAL = 60   # seconds between periodic cleanup sweeps


def cleanup_expired():
    """Remove offline clients and expired transfers. Returns count of cleaned items."""
    now = time.time()
    cleaned = 0
    freed_bytes = 0
    with state_lock:
        # expire offline clients
        for user in list(clients.keys()):
            if now - clients[user]["last_seen"] > CLIENT_TIMEOUT:
                del clients[user]
                cleaned += 1

        # expire old transfers AND transfers with missing files
        for tid in list(transfers.keys()):
            t = transfers[tid]
            age = now - t["created_at"]
            file_path = t.get("file_path", "")
            file_exists = file_path and os.path.exists(file_path)

            # Clean if: expired, or file gone (stale), or already accepted/rejected for >60s
            should_clean = (
                age > TRANSFER_MAX_AGE
                or (t["status"] in ("accepted", "rejected") and age > 60)
                or (file_path and not file_exists)
            )
            if should_clean:
                if file_exists:
                    try:
                        size = os.path.getsize(file_path)
                        os.remove(file_path)
                        freed_bytes += size
                    except OSError:
                        pass
                del transfers[tid]
                cleaned += 1

    if cleaned:
        print(f"[🧹] Cleanup: removed {cleaned} items, freed {freed_bytes} bytes")
    return cleaned


def cleanup_loop():
    """Background thread that periodically cleans expired data."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            cleanup_expired()
        except Exception as e:
            print(f"[!] Cleanup error: {e}")


def json_response(handler, data, status=200):
    """Send a JSON response."""
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler, message, status=400):
    json_response(handler, {"status": "error", "message": message}, status)


# ── HTTP Request Handler ──────────────────────────────────────────────────
class FileShareHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        """Suppress default logging to stderr; use our own."""
        pass

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        # GET /api/clients
        if path == "/api/clients":
            cleanup_expired()
            with state_lock:
                client_list = [
                    {"username": u, "hostname": c["hostname"], "last_seen": c["last_seen"]}
                    for u, c in clients.items()
                ]
            json_response(self, {"status": "ok", "clients": client_list})

        # GET /api/pending/<username>
        elif path.startswith("/api/pending/"):
            username = path.split("/")[-1]
            cleanup_expired()
            with state_lock:
                user_transfers = [
                    {
                        "transfer_id": tid,
                        "from_user": t["from_user"],
                        "filename": t["filename"],
                        "file_size": t["file_size"],
                        "status": t["status"],
                    }
                    for tid, t in transfers.items()
                    if t["to_user"] == username
                ]
            json_response(self, {"status": "ok", "transfers": user_transfers})

        # GET /api/download/<transfer_id>
        elif path.startswith("/api/download/"):
            transfer_id = path.split("/")[-1]
            with state_lock:
                t = transfers.get(transfer_id)
            if not t:
                error_response(self, "Transfer not found", 404)
                return
            if t["status"] != "accepted":
                error_response(self, "Transfer not yet accepted", 403)
                return
            file_path = t.get("file_path", "")
            if not file_path or not os.path.exists(file_path):
                error_response(self, "File not found on server", 404)
                return

            file_size = os.path.getsize(file_path)
            safe_filename = t["filename"]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{safe_filename}"')
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            with open(file_path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

            # Auto-cleanup after successful download to free router storage
            with state_lock:
                if transfer_id in transfers:
                    transfers[transfer_id]["status"] = "downloaded"
            try:
                os.remove(file_path)
            except OSError:
                pass
            with state_lock:
                transfers.pop(transfer_id, None)
            print(f"[✓] Transfer {transfer_id} downloaded and cleaned up")

        # GET /api/my-requests/<username>
        elif path.startswith("/api/my-requests/"):
            username = path.split("/")[-1]
            cleanup_expired()
            with state_lock:
                sent = [
                    {
                        "transfer_id": tid,
                        "to_user": t["to_user"],
                        "filename": t["filename"],
                        "file_size": t["file_size"],
                        "status": t["status"],
                    }
                    for tid, t in transfers.items()
                    if t["from_user"] == username
                ]
            json_response(self, {"status": "ok", "transfers": sent})

        else:
            error_response(self, "Not found", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        # POST /api/register
        if path == "/api/register":
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                error_response(self, "Invalid JSON")
                return
            username = data.get("username", "").strip()
            hostname = data.get("hostname", "").strip()
            if not username or not hostname:
                error_response(self, "username and hostname required")
                return
            with state_lock:
                clients[username] = {
                    "hostname": hostname,
                    "ip": self.client_address[0],
                    "last_seen": time.time(),
                }
            print(f"[+] Client registered: {username} @ {hostname}")
            json_response(self, {"status": "ok", "username": username})

        # POST /api/heartbeat
        elif path == "/api/heartbeat":
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                error_response(self, "Invalid JSON")
                return
            username = data.get("username", "").strip()
            if not username:
                error_response(self, "username required")
                return
            with state_lock:
                if username in clients:
                    clients[username]["last_seen"] = time.time()
                    clients[username]["ip"] = self.client_address[0]
                    json_response(self, {"status": "ok"})
                else:
                    json_response(self, {"status": "re-register", "message": "Client not registered"})

        # POST /api/send-request
        elif path == "/api/send-request":
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                error_response(self, "Invalid JSON")
                return
            from_user = data.get("from_user", "").strip()
            to_user = data.get("to_user", "").strip()
            filename = data.get("filename", "").strip()
            file_size = data.get("file_size", 0)
            if not all([from_user, to_user, filename]):
                error_response(self, "from_user, to_user, filename required")
                return

            transfer_id = str(uuid.uuid4())[:8]
            with state_lock:
                transfers[transfer_id] = {
                    "transfer_id": transfer_id,
                    "from_user": from_user,
                    "to_user": to_user,
                    "filename": filename,
                    "file_size": file_size,
                    "status": "awaiting_upload",
                    "created_at": time.time(),
                    "file_path": "",
                }
            print(f"[→] Transfer {transfer_id}: {from_user} → {to_user}: {filename} ({file_size} bytes)")
            json_response(self, {"status": "ok", "transfer_id": transfer_id})

        # POST /api/upload/<transfer_id>
        elif path.startswith("/api/upload/"):
            transfer_id = path.split("/")[-1]
            with state_lock:
                t = transfers.get(transfer_id)
            if not t:
                error_response(self, "Transfer not found", 404)
                return
            if t["status"] != "awaiting_upload":
                error_response(self, f"Transfer in wrong state: {t['status']}", 400)
                return

            os.makedirs(STORE_DIR, exist_ok=True)
            safe_filename = f"{transfer_id}_{t['filename']}"
            file_path = os.path.join(STORE_DIR, safe_filename)

            # Write uploaded data
            with open(file_path, "wb") as f:
                f.write(body)

            actual_size = os.path.getsize(file_path)
            with state_lock:
                t["file_path"] = file_path
                t["file_size"] = actual_size
                t["status"] = "pending"
                t["created_at"] = time.time()  # reset expiry from now

            print(f"[↑] Uploaded {transfer_id}: {actual_size} bytes")
            json_response(self, {"status": "ok", "transfer_id": transfer_id, "file_size": actual_size})

        # POST /api/respond/<transfer_id>
        elif path.startswith("/api/respond/"):
            transfer_id = path.split("/")[-1]
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                error_response(self, "Invalid JSON")
                return
            action = data.get("action", "").strip()
            if action not in ("accept", "reject"):
                error_response(self, "action must be 'accept' or 'reject'")
                return

            with state_lock:
                t = transfers.get(transfer_id)
                if not t:
                    error_response(self, "Transfer not found", 404)
                    return
                if action == "accept":
                    t["status"] = "accepted"
                    print(f"[✓] Transfer {transfer_id} accepted by {t['to_user']}")
                else:
                    t["status"] = "rejected"
                    if t.get("file_path") and os.path.exists(t["file_path"]):
                        os.remove(t["file_path"])
                    print(f"[✗] Transfer {transfer_id} rejected by {t['to_user']}")

            json_response(self, {"status": "ok", "transfer_id": transfer_id, "action": action})

        else:
            error_response(self, "Not found", 404)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/transfer/"):
            transfer_id = path.split("/")[-1]
            with state_lock:
                t = transfers.pop(transfer_id, None)
            if t and t.get("file_path") and os.path.exists(t["file_path"]):
                os.remove(t["file_path"])
            json_response(self, {"status": "ok", "deleted": transfer_id})
        else:
            error_response(self, "Not found", 404)


# ── Server startup ────────────────────────────────────────────────────────
def main():
    os.makedirs(STORE_DIR, exist_ok=True)

    # Start background cleanup thread (daemon = dies with main process)
    cleaner = threading.Thread(target=cleanup_loop, daemon=True, name="cleanup")
    cleaner.start()

    server = http.server.HTTPServer((BIND_HOST, BIND_PORT), FileShareHandler)
    print(f"GL-FileShare Server listening on {BIND_HOST}:{BIND_PORT}")
    print(f"Storage: {STORE_DIR}")
    print(f"Cleanup: every {CLEANUP_INTERVAL}s, transfers expire after {TRANSFER_MAX_AGE}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
