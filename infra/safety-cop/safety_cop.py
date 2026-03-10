#!/usr/bin/env python3
"""
openclaw-safety-cop v2: root-level monitor with Claude assessment.
- Polls file hashes every 5s
- Asks Claude Code (as ophirsw) if changes are security concerns
- Only alerts Signal group if Claude says YES; logs otherwise
- Auto-reverts dnsmasq.conf on unauthorized change (always, before asking Claude)
- Monitors docker container events (always alerts — no Claude needed)
- Creates Signal group "OpenClaw Security Alerts" on first start
"""

import difflib, grp, hashlib, json, logging, os, pwd, queue, shutil, subprocess
import sys, threading, time
from datetime import datetime, timezone
from pathlib import Path

# ── config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL  = 5      # seconds between file polls
REVERT_WINDOW  = 60     # seconds: approved change must appear within this window
RETRY_INTERVAL = 30     # seconds between Signal send retries
MAX_RETRIES    = 10
CLAUDE_TIMEOUT = 60     # seconds to wait for Claude assessment
MAX_CONTENT    = 50_000 # bytes of file content to store for diffing

SIGNAL_SCRIPT  = "/opt/openclaw-safety-cop/send_signal.sh"
GOOD_CONF_PATH = Path("/opt/openclaw-safety-cop/dnsmasq.conf.good")
GROUP_ID_FILE  = Path("/opt/openclaw-safety-cop/group_id.txt")
APPROVALS_FILE = Path("/tmp/openclaw-processed-approvals")
LOG_FILE       = "/var/log/openclaw-safety-cop.log"
GATEWAY        = "openclaw-gateway"
FALLBACK_DM    = "+14084758230"

CLAUDE_BIN     = "/home/ophirsw/.local/bin/claude"
OPHIRSW_HOME   = "/home/ophirsw"

WATCHED = {
    "/home/ophirsw/openclaw-dns/dnsmasq.conf": {
        "revert": True,
        "msg": "dnsmasq.conf modified outside approved flow — REVERTED",
    },
    "/home/ophirsw/.openclaw/workspace/SOUL.md": {
        "revert": False,
        "msg": "SOUL.md was modified",
    },
    "/home/ophirsw/openclaw-dns/openclaw-monitor.sh": {
        "revert": False,
        "msg": "openclaw-monitor.sh was modified",
    },
    "/home/ophirsw/openclaw-dns/sse-watcher.py": {
        "revert": False,
        "msg": "sse-watcher.py was modified",
    },
}

DOCKER_ALERTS = {
    ("container", "stop"):    "openclaw-gateway was STOPPED",
    ("container", "kill"):    "openclaw-gateway received KILL",
    ("container", "destroy"): "openclaw-gateway was DESTROYED",
    ("container", "pause"):   "openclaw-gateway was PAUSED",
}

# ── logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("safety_cop")
log.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                         datefmt="%Y-%m-%dT%H:%M:%SZ")
for _h in (logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stderr)):
    _h.setFormatter(_fmt)
    log.addHandler(_h)

# ── signal group ──────────────────────────────────────────────────────────────
_group_id: str | None = None

def _rpc_call(payload: dict) -> dict:
    """Send a JSON-RPC call to signal-cli inside the gateway container."""
    tmpfile = f"/tmp/signal_rpc_{os.getpid()}_{int(time.time())}.json"
    try:
        with open(tmpfile, "w") as f:
            json.dump(payload, f)
        subprocess.run(["docker", "cp", tmpfile, f"{GATEWAY}:/tmp/sig-rpc.json"],
                       check=True, capture_output=True, timeout=10)
        result = subprocess.run(
            ["docker", "exec", GATEWAY,
             "curl", "-sf", "-X", "POST", "http://127.0.0.1:8080/api/v1/rpc",
             "-H", "Content-Type: application/json",
             "-d", "@/tmp/sig-rpc.json"],
            capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)
    finally:
        try:
            os.unlink(tmpfile)
        except FileNotFoundError:
            pass

def ensure_group_id() -> str | None:
    global _group_id
    if GROUP_ID_FILE.exists():
        gid = GROUP_ID_FILE.read_text().strip()
        if gid:
            _group_id = gid
            log.info("Loaded Signal group: %s...", gid[:12])
            return gid
    log.info("Creating Signal group 'OpenClaw Security Alerts'...")
    try:
        resp = _rpc_call({
            "jsonrpc": "2.0",
            "method": "updateGroup",
            "params": {"name": "OpenClaw Security Alerts", "members": [FALLBACK_DM]},
            "id": 1,
        })
        gid = resp.get("result", {}).get("groupId")
        if not gid:
            raise ValueError(f"No groupId in response: {resp}")
        GROUP_ID_FILE.write_text(gid)
        os.chmod(str(GROUP_ID_FILE), 0o600)
        _group_id = gid
        log.info("Created Signal group: %s...", gid[:12])
        return gid
    except Exception as e:
        log.error("Failed to create Signal group: %s — will DM instead", e)
        return None

# ── signal alerting ───────────────────────────────────────────────────────────
# Queue items: (target_type, target_value, message_text)
_q: queue.Queue = queue.Queue()

def send_alert(msg: str, urgent: bool = False):
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    full = f"🚨🔥🚨🔥🚨🔥🚨🔥🚨🔥\n[SAFETY COP {ts}]\n{msg}" if urgent else f"[SAFETY COP {ts}]\n{msg}"
    log.warning("ALERT: %s", msg[:200])
    if _group_id:
        _q.put(("group", _group_id, full))
    else:
        _q.put(("recipient", FALLBACK_DM, full))

def _send_signal(target_type: str, target_value: str, msg: str):
    """Send a Signal message via the gateway's signal-cli JSON-RPC.
    Uses _rpc_call directly (Python) — no shell script, no quoting issues."""
    params: dict = {"message": msg}
    if target_type == "group":
        params["groupId"] = target_value
    else:
        params["recipient"] = target_value
    resp = _rpc_call({"jsonrpc": "2.0", "method": "send", "params": params, "id": 1})
    if "error" in resp:
        raise RuntimeError(f"RPC error: {resp['error']}")
    results = resp.get("result", {}).get("results", [])
    failed = [r for r in results if r.get("type") != "SUCCESS"]
    if failed:
        raise RuntimeError(f"Delivery failed: {failed}")

def _alert_thread():
    while True:
        target_type, target_value, msg = _q.get()
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                _send_signal(target_type, target_value, msg)
                log.info("Alert sent (attempt %d) to %s %s...",
                         attempt, target_type, target_value[:12])
                break
            except Exception as e:
                log.warning("Send failed attempt %d/%d: %s", attempt, MAX_RETRIES, e)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_INTERVAL)
        _q.task_done()

# ── claude assessment ─────────────────────────────────────────────────────────
def _drop_to_ophirsw():
    """preexec_fn: drop supplementary groups, gid, uid to ophirsw before exec."""
    pw = pwd.getpwnam("ophirsw")
    supplementary = [g.gr_gid for g in grp.getgrall() if pw.pw_name in g.gr_mem]
    supplementary.append(pw.pw_gid)
    os.setgroups(supplementary)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)

def ask_claude(path: str, diff: str) -> tuple[bool, str]:
    """Ask Claude Code if a file change is a security concern.
    Returns (is_concern, assessment_text).
    On timeout/error, defaults to True (alert) for safety."""
    prompt = f"""\
You are the safety cop for an AI assistant system called OpenClaw running on a Linux server.

System context:
- openclaw-gateway: Docker container running an AI assistant (Claude) that responds to Signal messages
- dnsmasq.conf: DNS allowlist — controls which domains the AI container can reach. Unauthorized
  changes could redirect traffic, add exfiltration routes, or disable the allowlist entirely.
- SOUL.md: The AI assistant's core identity/instructions file. Modification could alter its
  behavior, remove safety constraints, or install hidden instructions.
- openclaw-monitor.sh / sse-watcher.py: Security monitoring scripts. Modification could blind
  the monitoring system, suppress alerts, or create backdoors in the alert pipeline.

A file was unexpectedly modified (no Signal approval was received):
  File: {path}

Diff:
{diff[:3000]}

Assess whether this is a genuine security concern. Be a strict cop — when in doubt, flag it.

Known benign patterns (SECURITY_CONCERN: NO):
- Whitespace-only or comment-only changes with no functional effect
- Adding a comment that explains existing config
- A change that clearly matches routine system maintenance with no attack vector

Red flags (SECURITY_CONCERN: YES):
- Any new server= line in dnsmasq.conf (new outbound DNS route)
- Removal or weakening of any allowlist/blocklist entry
- Any change to SOUL.md instructions, especially removing constraints or adding new capabilities
- Any change to monitoring scripts that suppresses, delays, or redirects alerts
- Any added shell commands, network calls, or file writes in monitoring scripts
- Anything that looks like it was written by an AI trying to test its own boundaries

Reply in exactly this format (nothing else):
SECURITY_CONCERN: YES
<1-3 sentence explanation of the specific risk>

or:

SECURITY_CONCERN: NO
<1-2 sentence explanation of why it is benign>
"""
    pw = pwd.getpwnam("ophirsw")
    env = {
        "HOME":    pw.pw_dir,
        "USER":    pw.pw_name,
        "LOGNAME": pw.pw_name,
        "PATH":    f"{pw.pw_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "TERM":    "dumb",
    }
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
            preexec_fn=_drop_to_ophirsw, env=env,
        )
        response = (result.stdout or result.stderr or "").strip()
        if not response:
            raise ValueError("Empty response from claude")
        log.info("Claude on %s: %s", os.path.basename(path), response[:300])
        is_concern = "SECURITY_CONCERN: YES" in response
        return is_concern, response
    except subprocess.TimeoutExpired:
        log.warning("Claude timed out assessing %s — defaulting to alert", path)
        return True, "(Claude timed out — alerting by default)"
    except Exception as e:
        log.warning("Claude error assessing %s: %s — alerting by default", path, e)
        return True, f"(Claude error: {e} — alerting by default)"

def assess_and_alert(path: str, diff: str, msg_prefix: str):
    """Background thread: ask Claude, then alert to group or log silently."""
    is_concern, assessment = ask_claude(path, diff)
    if is_concern:
        send_alert(f"{msg_prefix}\n\nClaude assessment:\n{assessment}", urgent=True)
    else:
        log.info("Not a security concern per Claude (%s): %s",
                 os.path.basename(path), assessment[:200])

# ── file integrity ────────────────────────────────────────────────────────────
def sha256(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None

def read_content(path: str) -> str | None:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(MAX_CONTENT)
    except FileNotFoundError:
        return None

def make_diff(path: str, old: str | None, new: str | None) -> str:
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = (new or "").splitlines(keepends=True)
    lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{path} (before)",
        tofile=f"{path} (after)",
        n=3,
    ))
    return "".join(lines)[:4000] if lines else "(no textual diff available)"

# Approval timestamps already consumed for a change — prevents one approval
# covering multiple consecutive changes within the REVERT_WINDOW.
_used_approvals: set[str] = set()

def consume_approval() -> bool:
    """Find an unused recent approval and mark it consumed. Returns True if found.
    NOTE: timestamps in the file are Signal ms-since-epoch, divide by 1000.
    Each approval token is one-time-use: a single Signal approval covers exactly
    one dnsmasq.conf change, even if another change occurs within REVERT_WINDOW."""
    try:
        lines = APPROVALS_FILE.read_text().splitlines()
    except FileNotFoundError:
        return False
    now = time.time()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ts_sec = float(line) / 1000.0
            if now - ts_sec <= REVERT_WINDOW and line not in _used_approvals:
                _used_approvals.add(line)
                log.info("Consumed approval token %s", line)
                return True
        except ValueError:
            continue
    return False

_hashes:   dict[str, str | None] = {}
_contents: dict[str, str | None] = {}

def init_hashes():
    for path in WATCHED:
        _hashes[path]   = sha256(path)
        _contents[path] = read_content(path)
        log.info("hash[%s] = %s...", path, (_hashes[path] or "MISSING")[:20])
    dpath = "/home/ophirsw/openclaw-dns/dnsmasq.conf"
    if _hashes[dpath] and not GOOD_CONF_PATH.exists():
        shutil.copy2(dpath, str(GOOD_CONF_PATH))
        os.chmod(str(GOOD_CONF_PATH), 0o600)
        log.info("Seeded dnsmasq.conf.good")

def revert_dnsmasq(path: str):
    if not GOOD_CONF_PATH.exists():
        send_alert("dnsmasq.conf changed — NO BACKUP to revert from. Manual check needed.")
        return
    try:
        shutil.copy2(str(GOOD_CONF_PATH), path)
        pw = pwd.getpwnam("ophirsw")
        os.chown(path, pw.pw_uid, pw.pw_gid)
        os.chmod(path, 0o644)   # dnsmasq container must be able to read it
        log.info("Reverted dnsmasq.conf")
    except Exception as e:
        send_alert(f"REVERT FAILED: {e}. Manual check needed.")

def poll():
    dpath = "/home/ophirsw/openclaw-dns/dnsmasq.conf"
    for path, cfg in WATCHED.items():
        cur  = sha256(path)
        last = _hashes.get(path)
        if cur == last:
            continue
        log.info("Change detected in %s", path)
        new_content = read_content(path)
        diff        = make_diff(path, _contents.get(path), new_content)

        if cfg["revert"] and path == dpath:
            if consume_approval():
                # Approved via Signal flow — update good backup and confirm
                if new_content:
                    shutil.copy2(path, str(GOOD_CONF_PATH))
                    os.chmod(str(GOOD_CONF_PATH), 0o600)  # backup stays private
                    os.chmod(path, 0o644)                  # live file must be container-readable
                    log.info("Approved change — updated dnsmasq.conf.good")
                send_alert("dnsmasq.conf updated via approved Signal flow.")
                _hashes[path]   = cur
                _contents[path] = new_content
            else:
                # Unauthorized — revert immediately, then assess asynchronously
                revert_dnsmasq(path)
                _hashes[path]   = sha256(path)
                _contents[path] = read_content(path)
                threading.Thread(
                    target=assess_and_alert,
                    args=(path, diff, cfg["msg"]),
                    daemon=True,
                ).start()
        else:
            # Not a revert target — update state and ask Claude
            _hashes[path]   = cur
            _contents[path] = new_content
            threading.Thread(
                target=assess_and_alert,
                args=(path, diff, cfg["msg"]),
                daemon=True,
            ).start()

# ── docker events ─────────────────────────────────────────────────────────────
def _docker_thread():
    while True:
        try:
            proc = subprocess.Popen(
                ["docker", "events", "--format", "{{json .}}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                name = ev.get("Actor", {}).get("Attributes", {}).get("name", "")
                if name != GATEWAY:
                    continue
                key = (ev.get("Type", ""), ev.get("Action", ""))
                if key in DOCKER_ALERTS:
                    send_alert(f"Docker: {DOCKER_ALERTS[key]}")
            proc.wait()
        except Exception as e:
            log.error("docker events error: %s", e)
        time.sleep(10)

# ── SSH guard — block OpenClaw from SSH/SCP to Vector robot ──────────────────
VECTOR_IP = "192.168.1.71"
VECTOR_SSH_PORT = 22
_ssh_guard_alerted: set = set()

def _ssh_guard_thread():
    """Monitor for SSH/SCP connections to Vector robot from OpenClaw-related processes.

    Checks /proc/net/tcp for established connections to Vector:22.
    If the owning process is part of the OpenClaw tree (node, openclaw),
    alert and attempt to kill the connection.
    """
    import struct

    def _hex_ip_port(hex_str):
        """Parse /proc/net/tcp hex address:port."""
        ip_hex, port_hex = hex_str.split(":")
        # /proc/net/tcp uses little-endian hex IP
        ip_int = int(ip_hex, 16)
        ip = f"{ip_int & 0xff}.{(ip_int >> 8) & 0xff}.{(ip_int >> 16) & 0xff}.{(ip_int >> 24) & 0xff}"
        port = int(port_hex, 16)
        return ip, port

    def _get_cmdline(pid):
        try:
            return Path(f"/proc/{pid}/cmdline").read_text().replace("\x00", " ").strip()
        except (FileNotFoundError, PermissionError):
            return ""

    while True:
        try:
            tcp_data = Path("/proc/net/tcp").read_text()
            for line in tcp_data.splitlines()[1:]:  # skip header
                fields = line.split()
                if len(fields) < 10:
                    continue
                # fields[2] = remote_address, fields[3] = state (01=ESTABLISHED)
                if fields[3] != "01":
                    continue
                remote_ip, remote_port = _hex_ip_port(fields[2])
                if remote_ip == VECTOR_IP and remote_port == VECTOR_SSH_PORT:
                    # Found SSH connection to Vector robot — check who owns it
                    inode = fields[9]
                    # Find PID owning this socket
                    pid = None
                    for proc_dir in Path("/proc").iterdir():
                        if not proc_dir.name.isdigit():
                            continue
                        try:
                            for fd in (proc_dir / "fd").iterdir():
                                try:
                                    link = os.readlink(str(fd))
                                    if f"socket:[{inode}]" in link:
                                        pid = int(proc_dir.name)
                                        break
                                except (FileNotFoundError, PermissionError):
                                    continue
                        except (FileNotFoundError, PermissionError):
                            continue
                        if pid:
                            break

                    if pid:
                        cmdline = _get_cmdline(pid)
                        # Alert on any SSH to Vector (the agent-loop has its own lock scripts)
                        alert_key = f"{pid}:{inode}"
                        if alert_key not in _ssh_guard_alerted:
                            _ssh_guard_alerted.add(alert_key)
                            # Only alert for OpenClaw-related processes
                            openclaw_indicators = ("openclaw", "node ", "signal", "robot-commands")
                            if any(ind in cmdline.lower() for ind in openclaw_indicators):
                                log.warning("SSH GUARD: OpenClaw process PID %d connected to Vector SSH! cmd: %s", pid, cmdline[:200])
                                send_alert(f"🚨 SSH GUARD: OpenClaw process (PID {pid}) opened SSH to Vector! Killing. cmd: {cmdline[:100]}")
                                try:
                                    os.kill(pid, 9)  # SIGKILL
                                    log.warning("SSH GUARD: killed PID %d", pid)
                                except (ProcessLookupError, PermissionError) as e:
                                    log.error("SSH GUARD: failed to kill PID %d: %s", pid, e)
        except Exception as e:
            log.error("SSH guard error: %s", e)
        time.sleep(POLL_INTERVAL)


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("safety-cop v2 starting (pid=%d)", os.getpid())
    GOOD_CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
    ensure_group_id()
    init_hashes()
    threading.Thread(target=_alert_thread,  daemon=True, name="alerts").start()
    threading.Thread(target=_docker_thread, daemon=True, name="docker").start()
    threading.Thread(target=_ssh_guard_thread, daemon=True, name="ssh-guard").start()
    send_alert("Safety cop v2 started. Claude assessments + SSH guard enabled.")
    while True:
        try:
            poll()
        except Exception as e:
            log.error("poll error: %s", e)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
