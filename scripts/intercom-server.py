#!/usr/bin/env python3
"""DEPRECATED: Signal intercom functionality has been merged into the bridge server
(apps/vector/bridge/routes.py) as /signal/send, /signal/send-image, /signal/send-camera.
The bridge also exposes /intercom/receive and /intercom/photo as backward-compatible aliases.
This standalone server is kept temporarily for reference and will be removed.

Original description:
NUC-side HTTP server for robot intercom — receives text/photo from robot, sends to Signal.

Endpoints:
    POST /intercom/receive  {"text": "..."}        → sends text to Ophir's Signal DM
    POST /intercom/photo    {"caption": "..."}      → fetches robot camera frame, sends as Signal attachment

Usage:
    python3 scripts/intercom-server.py

Environment:
    PORT              — listen port (default: 8095)
    SIGNAL_RECIPIENT  — Signal number for DM (default: Ophir)
    SIGNAL_GROUP_ID   — fallback group if no recipient
    BOT_CONTAINER     — openclaw-gateway container (default: openclaw-gateway)
    BRIDGE_URL        — robot bridge base URL (default: http://192.168.1.71:8081)
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen

PORT = int(os.environ.get("PORT", "8095"))
SIGNAL_RECIPIENT = os.environ.get("SIGNAL_RECIPIENT", "+14084758230")
SIGNAL_GROUP_ID = os.environ.get(
    "SIGNAL_GROUP_ID", "BUrA+nRRpsfdYgftby/jpJ7Ugy5PBzYWg89oNNr4nF4="
)
BOT_CONTAINER = os.environ.get("BOT_CONTAINER", "openclaw-gateway")
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://192.168.1.71:8081")

_last_sent = 0.0
COOLDOWN_SECONDS = 3


def send_signal(text, attachment_path=None):
    """Send message to Signal via openclaw-gateway JSON-RPC."""
    global _last_sent
    now = time.time()
    if now - _last_sent < COOLDOWN_SECONDS:
        print(f"[intercom] cooldown, skipping: {text}", flush=True)
        return False
    _last_sent = now

    if SIGNAL_RECIPIENT:
        params = {"recipient": SIGNAL_RECIPIENT, "message": text}
    else:
        params = {"groupId": SIGNAL_GROUP_ID, "message": text}

    container = shlex.quote(BOT_CONTAINER)

    # Copy attachment into container if present
    container_attachment = None
    if attachment_path:
        container_attachment = f"/tmp/{os.path.basename(attachment_path)}"
        cp = subprocess.run(
            ["sg", "docker", "-c",
             f"docker cp {shlex.quote(attachment_path)} {container}:{container_attachment}"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            print(f"[intercom] docker cp failed: {cp.stderr}", file=sys.stderr, flush=True)
            container_attachment = None

    if container_attachment:
        params["attachments"] = [container_attachment]

    payload = json.dumps({"jsonrpc": "2.0", "method": "send", "params": params, "id": 1})

    try:
        result = subprocess.run(
            ["sg", "docker", "-c",
             f"docker exec -i {container} curl -sf -X POST "
             "http://127.0.0.1:8080/api/v1/rpc "
             "-H 'Content-Type: application/json' -d @-"],
            input=payload, capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            suffix = " [+attachment]" if container_attachment else ""
            print(f"[intercom] sent: {text}{suffix}", flush=True)
            return True
        print(f"[intercom] send failed: {result.stderr}", file=sys.stderr, flush=True)
        return False
    except Exception as exc:
        print(f"[intercom] send error: {exc}", file=sys.stderr, flush=True)
        return False


def fetch_photo():
    """Fetch JPEG from robot bridge /capture."""
    try:
        url = BRIDGE_URL.rstrip("/") + "/capture"
        with urlopen(url, timeout=10) as resp:
            if resp.status == 200:
                data = resp.read()
                if len(data) > 1000:
                    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", prefix="robot-", delete=False)
                    tmp.write(data)
                    tmp.close()
                    return tmp.name
    except Exception as exc:
        print(f"[intercom] capture failed: {exc}", file=sys.stderr, flush=True)
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[intercom] {args[0]}", flush=True)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _respond(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            data = self._read_json()
        except Exception:
            self._respond(400, {"error": "invalid JSON"})
            return

        if self.path == "/intercom/receive":
            text = str(data.get("text", "")).strip()
            if not text:
                self._respond(400, {"error": "text required"})
                return
            ok = send_signal(f"\U0001f916 Robot says: {text}")
            self._respond(200 if ok else 502, {"status": "sent" if ok else "failed"})

        elif self.path == "/intercom/photo":
            caption = str(data.get("caption", "Photo from robot")).strip()
            photo = fetch_photo()
            if photo:
                ok = send_signal(f"\U0001f4f8 {caption}", attachment_path=photo)
                try:
                    os.unlink(photo)
                except OSError:
                    pass
                self._respond(200 if ok else 502, {"status": "sent" if ok else "failed"})
            else:
                self._respond(502, {"error": "capture failed"})

        elif self.path == "/intercom/send-image":
            caption = str(data.get("caption", "")).strip()
            image_path = str(data.get("path", "")).strip()
            if not image_path or not os.path.isfile(image_path):
                self._respond(400, {"error": "path required and must exist"})
                return
            ok = send_signal(caption or "\U0001f4f8 Image", attachment_path=image_path)
            self._respond(200 if ok else 502, {"status": "sent" if ok else "failed"})

        else:
            self._respond(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


def main():
    server = ReusableHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[intercom] listening on :{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[intercom] shutting down", flush=True)
    server.server_close()


if __name__ == "__main__":
    main()
