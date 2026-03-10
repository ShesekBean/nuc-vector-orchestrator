# OpenClaw Configuration — Full Reference

Last updated: 2026-03-10

---

## Overview

Two OpenClaw gateway instances run on the NUC ("desk"), each as a Docker container managed by systemd user units.

| Instance | Container | Signal Number | Ports (host) | Config Dir | Purpose |
|----------|-----------|---------------|--------------|------------|---------|
| **Shon** (primary) | `openclaw-gateway` | +14086469950 | 18889, 18890 | `~/.openclaw/` | Robot control, Ophir's personal assistant |
| **Ade** (work bot) | `openclaw-ade` | (separate number) | 18891, 18892 | `~/.openclaw-ade/` | Work-related bot |

Both use the same Docker image: `openclaw:signal` (custom-built, v2026.2.26).

---

## Docker Image

**Image:** `openclaw:signal` — custom build based on OpenClaw with Java + signal-cli baked in.

**CRITICAL:** The generic `ghcr.io/openclaw/openclaw:latest` does NOT include signal-cli or Java. It CANNOT be used for Signal integration. The custom image is required.

| Tag | Image ID | Size | Notes |
|-----|----------|------|-------|
| `openclaw:signal` | 15c233e18b66 | 4.78GB | Production (v2026.2.26 + signal-cli) |
| `openclaw:signal-old` | 15c233e18b66 | 4.78GB | Backup tag (same image) |
| `openclaw:local` | 16507faafbe4 | 4.38GB | Unknown / older build |
| `ghcr.io/openclaw/openclaw:latest` | 7b1294f6aa2e | 3.82GB | Generic (NO signal-cli — DO NOT USE) |

**Auto-update is DISABLED** in config to prevent accidental replacement with generic image.

---

## Run Scripts

Located at `~/Documents/claude/openclaw/` (also backed up at `infra/openclaw/run-scripts/`).

### openclaw-gateway-run.sh
```bash
docker run --rm \
  --name openclaw-gateway \
  -e HOME=/home/node \
  -e TERM=xterm-256color \
  -e OPENCLAW_GATEWAY_TOKEN=<token> \
  -e OPENCLAW_PREFER_PNPM=1 \
  -e NODE_ENV=production \
  -v openclaw_signal_cli_data:/home/node/.local/share/signal-cli \
  -v ~/.openclaw:/home/node/.openclaw \
  -v ~/.openclaw/workspace:/home/node/.openclaw/workspace \
  -p 127.0.0.1:18889:18789 \
  -p 127.0.0.1:18890:18790 \
  openclaw:signal \
  node dist/index.js gateway --bind lan --port 18789
```

### openclaw-ade-run.sh
Same pattern, different container name, ports (18891/18892), config dir (`~/.openclaw-ade`), and gateway token.

---

## Systemd Units

All are user units at `~/.config/systemd/user/`. Manage with `systemctl --user`.

### Services
| Unit | Description | Status |
|------|-------------|--------|
| `openclaw-gateway.service` | Shon gateway (Signal) | active |
| `openclaw-ade.service` | Ade gateway (work bot) | active |
| `openclaw-monitor.service` | Security monitor (Shon) — tails logs for rejections | active |
| `openclaw-monitor-ade.service` | Security monitor (Ade) | active |
| `openclaw-backup.service` | Config backup (Shon) | triggered by timer |
| `openclaw-ade-backup.service` | Config backup (Ade) | triggered by timer |
| `openclaw-backup-monthly.service` | Monthly full backup (Shon) | triggered by timer |
| `openclaw-ade-backup-monthly.service` | Monthly full backup (Ade) | triggered by timer |

### Timers
| Timer | Interval | Backup Location |
|-------|----------|-----------------|
| `openclaw-backup.timer` | Every 6 hours | `~/openclaw-backups/` (GPG encrypted) |
| `openclaw-ade-backup.timer` | Every 1 hour | `~/openclaw-ade-backups/` (GPG encrypted) |
| `openclaw-backup-monthly.timer` | 1st of month | `~/openclaw-backups/monthly/` |
| `openclaw-ade-backup-monthly.timer` | 1st of month | `~/openclaw-ade-backups/monthly/` |

---

## Docker Volumes

| Volume | Mount Path | Contents |
|--------|------------|----------|
| `openclaw_signal_cli_data` | `/home/node/.local/share/signal-cli` | Signal protocol state, keys, attachments |
| `openclaw_ade_signal_cli_data` | (same path in ADE) | ADE Signal state |

**WARNING:** Losing `openclaw_signal_cli_data` means losing the Signal registration. Would need to re-register the phone number.

---

## Configuration (openclaw.json)

Config lives at `~/.openclaw/openclaw.json` (bind-mounted into container at `/home/node/.openclaw/openclaw.json`).

### Secrets
All API secrets are stored in `~/.openclaw/.env` (chmod 600) and referenced in config via `${VAR_NAME}`:
- `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`
- `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`, `WITHINGS_REFRESH_TOKEN`
- `OURA_ACCESS_TOKEN`

### Agent Defaults
- **Model:** `openai-codex/gpt-5.3-codex`
- **Max concurrent agents:** 4
- **Max concurrent subagents:** 8
- **Compaction:** safeguard mode
- **Sandbox:** `non-main` (secondary agents sandboxed, main Signal agent unsandboxed), read-only workspace

### Signal Channel
- **Account:** +14086469950 (Shon's Signal number)
- **signal-cli path:** `signal-cli` (on PATH inside the custom image)
- **DM policy:** `allowlist` — only Ophir's number (+14084758230) can DM
- **Group policy:** `disabled` — bot does not respond in group chats

### Gateway
- **Port:** 18789 (internal), mapped to 127.0.0.1:18889 (host)
- **Bind:** `loopback` in config (old image may ignore this, but Docker port mapping enforces localhost-only)
- **Auth:** token-based
- **Control UI origins:** localhost:18789, 127.0.0.1:18789, localhost:18889, 127.0.0.1:18889
- **Tailscale:** off

### Security Settings (hardened 2026-03-10)
| Setting | Value | Purpose |
|---------|-------|---------|
| `tools.elevated.enabled` | `false` | No elevated/sudo commands |
| `tools.fs.workspaceOnly` | `true` | Filesystem access limited to workspace |
| `tools.exec.timeoutSec` | `30` | Command execution timeout |
| `approvals.exec.enabled` | `true` | Exec approvals forwarded to Signal |
| `approvals.exec.targets` | Ophir via Signal | Approval requests go to +14084758230 |
| `discovery.mdns.mode` | `minimal` | Reduced network info leakage |
| `logging.redactSensitive` | `tools` | Sensitive data redacted in tool logs |
| `hooks.defaultSessionKey` | `hook:ingress` | Scoped hook sessions |
| `browser.ssrfPolicy.allowedHostnames` | `[]` (empty) | Block browser access to private networks |
| `update.auto.enabled` | `false` | Prevent auto-update to generic image |

### Internal Hooks
- `session-memory` — persists memory across sessions
- `boot-md` — loads workspace MDs on startup
- `bootstrap-extra-files` — injects extra files into agent context
- `command-logger` — logs all commands

### Plugins
- `signal` — enabled (Signal channel plugin)

---

## Workspace

Located at `~/.openclaw/workspace/` (bind-mounted into container).

### Bootstrap MDs
| File | Purpose |
|------|---------|
| `SOUL.md` | Bot personality and tone |
| `AGENTS.md` | Agent configuration and routing |
| `IDENTITY.md` | Bot identity details |
| `USER.md` | User (Ophir) preferences and context |
| `TOOLS.md` | Available tools and capabilities |
| `BOOTSTRAP.md` | Startup instructions |
| `HEARTBEAT.md` | Health check / heartbeat config |

### Skills
| Skill | Trigger | Description |
|-------|---------|-------------|
| `robot-control` | "robot \<cmd\>" | Controls physical robot via HTTP bridge at 192.168.1.71:8081 |
| `fitness` | fitness-related messages | Strava/Withings/Oura integration |

### Memory
- `memory/fitness-log.json` — fitness tracking data
- Skills can read/write JSON files in `memory/`

---

## Network Architecture

```
Ophir's Phone (Signal app)
    │
    ▼
Signal servers (internet)
    │
    ▼
signal-cli daemon (inside openclaw-gateway container, port 8080)
    │
    ▼
OpenClaw gateway (processes message, invokes skill)
    │
    ▼ (robot-control skill triggers curl)
    │
HTTP to Jetson bridge (192.168.1.71:8081)
    │
    ▼
Robot hardware (motors, servos, camera, etc.)
```

### Ports (all on localhost only)
| Port | Service |
|------|---------|
| 18889 | Shon gateway WebSocket + Control UI |
| 18890 | Shon gateway secondary port |
| 18891 | Ade gateway WebSocket + Control UI |
| 18892 | Ade gateway secondary port |

### Access
- **Control UI:** `http://localhost:18889` (from NUC only, or via SSH tunnel)
- **SSH tunnel from laptop:** `ssh -L 18889:127.0.0.1:18889 ophirsw@<NUC-IP>`
- **Device pairing required** after container recreation: `docker exec openclaw-gateway openclaw devices list` then `openclaw devices approve <id>`

---

## Recovery Procedures

### Gateway down (container missing)
1. Check: `docker ps | grep openclaw-gateway`
2. Check systemd: `systemctl --user status openclaw-gateway.service`
3. Check logs: `journalctl --user -u openclaw-gateway.service --since "5 min ago"`
4. Fix WorkingDirectory if missing: `mkdir -p ~/Documents/claude/openclaw`
5. Restart: `systemctl --user restart openclaw-gateway.service`

### Signal not connecting
1. Check signal-cli inside container: `docker exec openclaw-gateway signal-cli --version`
2. Check data volume: `docker volume inspect openclaw_signal_cli_data`
3. If `exec format error` or `ENOENT` — wrong image (generic instead of custom)

### Config broken
1. Backups at `~/.openclaw/openclaw.json.bak*` (multiple generations)
2. Pre-hardening backup: `~/.openclaw/openclaw.json.bak.pre-hardening`
3. Encrypted backups in `~/openclaw-backups/`
4. Restore: `cp ~/.openclaw/openclaw.json.bak ~/.openclaw/openclaw.json && systemctl --user restart openclaw-gateway`

### Full disaster recovery
1. Restore `~/.openclaw/` from encrypted backup
2. Restore Docker volume from backup (if available)
3. Recreate run scripts from `infra/openclaw/run-scripts/`
4. Ensure `openclaw:signal` image exists (old custom build, NOT generic)
5. `systemctl --user restart openclaw-gateway`

---

## Security Audit

Run periodically:
```bash
docker exec openclaw-gateway openclaw security audit --deep
docker exec openclaw-gateway openclaw doctor
```

Last audit (2026-03-10): 0 critical, 1 warn (trusted_proxies — N/A for loopback), 2 info.

---

## Update Procedure

**DO NOT** simply `docker pull ghcr.io/openclaw/openclaw:latest` — the generic image lacks signal-cli.

To update OpenClaw:
1. Find or rebuild `Dockerfile.signal` (custom image with Java + signal-cli)
2. Build new image from latest OpenClaw base + signal-cli
3. Tag: `docker tag <new-id> openclaw:signal`
4. Keep old image: `docker tag openclaw:signal openclaw:signal-old`
5. Restart: `systemctl --user restart openclaw-gateway`
6. Verify Signal connects: check logs for `signal-cli: INFO DaemonCommand`
7. If broken, rollback: `docker tag openclaw:signal-old openclaw:signal && systemctl --user restart openclaw-gateway`
