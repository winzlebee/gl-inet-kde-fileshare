# GL-Slop-FileShare

## WARNING: This project is vibe-coded. Use at your own risk.

**We just made this to share big files between roommates at a share house with a Gl.iNet network share. We wanted a weird KDE notification thing so nothing else met our needs.**

This was predominantly created with DeepSeek Pro V4. Some cursory checks were made to make sure it didn't nuke my entire router, however please use at your own risk.

**LAN file sharing via your GL.iNet router (Flint 2)**

This project lets you easily send files between computers on your local
network, using the GL.iNet router as a central exchange hub. It consists of:

1. **Server** — a lightweight Python HTTP service that runs on the router
2. **Client** — a KDE system-tray application for sending and receiving files

---

## Architecture

```
┌──────────────┐          ┌──────────────────┐          ┌──────────────┐
│  Computer A  │  HTTP    │  GL.iNet Router   │  HTTP    │  Computer B  │
│  (KDE tray)  │◄────────►│  (Flint 2)        │◄────────►│  (KDE tray)  │
│              │          │  192.168.1.1:9090 │          │              │
│  alice       │          │  /tmp/filestore/  │          │  bob         │
└──────────────┘          └──────────────────┘          └──────────────┘
```

**Flow:**
1. All computers register with the router server
2. Alice selects a file → picks Bob from the online list → file uploads to router
3. Bob's tray client polls every 3s → discovers pending transfer → popup appears
4. Bob clicks "Accept" → file downloads from router → saved to Downloads
5. Bob clicks "Reject" → file is deleted from router → Alice gets notified

---

## Router Setup (GL.iNet Flint 2)

### Prerequisites

- SSH access to the router (`ssh root@192.168.1.1`)
- The router must have internet access to install Python 3 via `opkg`

### Quick Install

```bash
# Step 1: Copy files to the router
scp install-router.sh server/gl-fileshare-server.py root@192.168.1.1:/tmp/

# Step 2: SSH in and run the installer
ssh root@192.168.1.1
sh /tmp/install-router.sh
```

### What the installer does

1. Installs `python3-light` and dependencies via `opkg` (if not already present)
2. Copies the server script to `/usr/share/gl-fileshare/`
3. Creates an OpenWrt init.d service at `/etc/init.d/gl-fileshare`
4. Enables auto-start on boot
5. Opens port 9090 on the LAN firewall

### Manual management

```bash
# Start / Stop / Restart
/etc/init.d/gl-fileshare start
/etc/init.d/gl-fileshare stop
/etc/init.d/gl-fileshare restart

# Check status
/etc/init.d/gl-fileshare status

# View logs
logread | grep gl-fileshare

# Open firewall port manually (if needed)
iptables -I INPUT -p tcp --dport 9090 -j ACCEPT
```

### Firewall note

The server listens on **port 9090** on the LAN interface. If your router has a
strict firewall, you may need to open this port. The GL.iNet web UI has a
Firewall → Port Forwarding section where you can add port 9090/TCP on the
LAN zone.

---

## Client Setup (Arch Linux / KDE)

### Quick Install

```bash
# From the project root:
chmod +x install-client-arch.sh
./install-client-arch.sh
```

This script:
1. Installs `python-pyqt6 python-requests python-dbus` via pacman
2. Copies the tray client to `/opt/gl-fileshare/client/`
3. Creates an autostart entry at `~/.config/autostart/gl-fileshare-tray.desktop`
4. Launches the tray immediately if KDE is running

### Manual Install

```bash
# Install dependencies
sudo pacman -S python-pyqt6 python-requests python-dbus

# Copy client
sudo mkdir -p /opt/gl-fileshare/client
sudo cp client/gl-fileshare-tray.py /opt/gl-fileshare/client/
sudo chmod +x /opt/gl-fileshare/client/gl-fileshare-tray.py

# Autostart
cp client/gl-fileshare-tray.desktop ~/.config/autostart/
```

### Running

```bash
# Run manually (for testing)
python3 /opt/gl-fileshare/client/gl-fileshare-tray.py

# With custom server address
GL_FILESHARE_SERVER=http://192.168.1.1:9090 python3 gl-fileshare-tray.py

# With custom username
GL_FILESHARE_USER=myname python3 gl-fileshare-tray.py
```

### Environment Variables

| Variable               | Default                | Description                   |
|------------------------|------------------------|-------------------------------|
| `GL_FILESHARE_SERVER` | `http://192.168.1.1:9090` | Router server URL           |
| `GL_FILESHARE_USER`   | `$USER`                | Your display name             |
| `GL_FILESHARE_HOST`   | `hostname` output      | Your computer's hostname      |

---

## Usage

### Sending a File

1. Right-click the tray icon (blue circle with up/down arrows)
2. Select **"Send File..."**
3. Choose a file from the file picker
4. Select the recipient from the online clients list
5. Click **"Send"**
6. The file uploads to the router — the recipient will be notified

### Receiving a File

1. When someone sends you a file:
   - A KDE notification appears
   - A dialog pops up showing the sender, filename, and size
2. Click **"Accept"** to download the file
   - Choose a save location (defaults to `~/Downloads`)
3. Click **"Reject"** to decline
   - The sender is notified of the rejection

### Tray Menu

| Action              | Description                                      |
|---------------------|--------------------------------------------------|
| **Send File...**    | Pick a file and send to an online client         |
| **Check for Files** | Manually check for pending transfers             |
| **Status**          | Show connection status and online clients        |
| **Quit**            | Exit the application                             |

---

## File Storage

- **Primary**: `/mnt/sda1/gl-fileshare/` — if an external USB drive is attached and mounted
- **Fallback**: `/tmp/filestore/` — RAM disk (tmpfs), used when no drive is attached
  - ~400 MB free on Flint 2; files are lost on router reboot

### Cleanup guarantees

Files are **always** cleaned up — never left lingering on the router:

| Trigger | When |
|----------------------|------|
| Download complete | Immediately after the recipient downloads the file |
| Recipient rejects | Immediately on rejection |
| Expiry | After 10 minutes if not accepted/rejected |
| Background sweep | Every 60 seconds, a daemon thread removes expired/stale files |
| Manual DELETE | Sender can cancel a transfer at any time |

---

## Server API Reference

### Endpoints

| Method   | Path                          | Description                  |
|----------|-------------------------------|------------------------------|
| `POST`   | `/api/register`               | Register a client            |
| `POST`   | `/api/heartbeat`              | Keep-alive ping              |
| `GET`    | `/api/clients`                | List online clients          |
| `POST`   | `/api/send-request`           | Create a transfer request    |
| `POST`   | `/api/upload/<transfer_id>`   | Upload file data             |
| `POST`   | `/api/respond/<transfer_id>`  | Accept or reject a transfer  |
| `GET`    | `/api/download/<transfer_id>` | Download accepted file       |
| `GET`    | `/api/pending/<username>`     | List pending transfers       |
| `GET`    | `/api/my-requests/<username>` | List transfers I initiated   |
| `DELETE` | `/api/transfer/<transfer_id>` | Delete a transfer            |

### Transfer States

```
awaiting_upload → pending → accepted → (downloaded → cleaned up)
                          → rejected  → (cleaned up)
                          → (expired after 10 min)
```

---

## Troubleshooting

### Client can't connect to server

- Check the router is reachable: `ping 192.168.1.1`
- Check the server is running: `curl http://192.168.1.1:9090/api/clients`
- Check firewall on router: `ssh root@192.168.1.1 iptables -L -n | grep 9090`
- Try a different IP: set `GL_FILESHARE_SERVER` env var

### No other clients show up

- All computers must be on the same LAN and using the same router IP
- Each client automatically registers on startup with its `$USER` name
- If two users have the same username, only one will appear

### Files don't arrive

- Large files may take time — the progress bar shows upload/download status
- Check router disk space: `ssh root@192.168.1.1 df -h /tmp`
- Transfers expire after 10 minutes if not accepted

### Server won't start on router

- Check Python is installed: `ssh root@192.168.1.1 python3 --version`
- Run manually to see errors: `ssh root@192.168.1.1 python3 /usr/share/gl-fileshare/gl-fileshare-server.py`
- Port 9090 might be in use: `ssh root@192.168.1.1 netstat -tlnp | grep 9090`

---


## License

MIT License — feel free to modify and distribute.
