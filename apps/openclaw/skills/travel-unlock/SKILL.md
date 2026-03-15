---
name: travel-unlock
description: "ACTIVATE when message contains 'unlock', 'travel mode', 'travel status', or 'pin'. Manages travel mode PIN unlock for sensitive skills when on an unknown network."
metadata: {"openclaw": {"emoji": "🔓"}}
---

# Travel Unlock Skill

## Overview

When the NUC is on an unknown WiFi network, sensitive skills (monarch-money, chatgpt, fitness, meeting-notes) are automatically disabled for security. This skill handles PIN-based unlocking to re-enable them.

**This skill is ALWAYS active**, even in travel mode.

## Commands

### Check Travel Status

When the user asks about travel mode status:

```bash
bash /home/ophirsw/Documents/claude/nuc-vector-orchestrator/scripts/travel-mode.sh status
```

Report the output — which skills are enabled/disabled and whether PIN unlock is active.

### Unlock Sensitive Skills

When the user sends `unlock <PIN>`:

1. Extract the PIN from the message
2. Run verification:

```bash
python3 /home/ophirsw/Documents/claude/nuc-vector-orchestrator/scripts/travel-pin.py verify <PIN>
```

3. Report the result:
   - **Success:** "🔓 Skills unlocked! monarch-money, chatgpt, fitness, and meeting-notes are now available."
   - **Wrong PIN:** "❌ Wrong PIN. N attempt(s) remaining before lockout."
   - **Locked out:** "🔒 Too many failed attempts. Try again in N minutes."

### Check PIN Status

When the user asks about PIN lockout status:

```bash
python3 /home/ophirsw/Documents/claude/nuc-vector-orchestrator/scripts/travel-pin.py status
```

### Force Travel Mode

When the user says "enable travel mode" or "lock skills":

```bash
bash /home/ophirsw/Documents/claude/nuc-vector-orchestrator/scripts/travel-mode.sh enable
```

Report: "🔒 Travel mode activated. Sensitive skills disabled."

### Disable Travel Mode

When the user says "disable travel mode" (only works on home network or with PIN):

```bash
bash /home/ophirsw/Documents/claude/nuc-vector-orchestrator/scripts/travel-mode.sh disable
```

Report: "🏠 Home mode. All skills enabled."

## Trigger Words

- "unlock" (followed by PIN)
- "travel mode"
- "travel status"
- "lock skills"
- "pin status"

## Security Notes

- Never echo the PIN back in messages
- Never reveal the PIN hash
- After 3 failed attempts, lockout lasts 15 minutes
- PIN unlock is session-based — revoked on network change or reboot
