# Project Vector (Vector Edition) — Summary

**Last updated:** 2026-03-14
**Purpose:** Self-contained snapshot for onboarding new LLM sessions.

## What Is This?

Project Vector (Vector edition) is a distributed multi-agent robotics system where a robot (Anki/DDL Vector 2.0 with OSKR) codes itself, tests itself, and improves itself — with minimal human intervention.

Two machines coordinate via GitHub Issues in a single monorepo:
- **NUC "desk"** (Intel x86_64, Ubuntu) — orchestrator, Signal gateway, ALL inference + control
- **Vector 2.0** (Snapdragon 212, OSKR unlocked) — thin gRPC client, hardware only

**Repo:** `ShesekBean/nuc-vector-orchestrator` (monorepo)
**Parent:** `ShesekBean/nuc-orchestrator` (R3 robot — reference architecture, archived)
**Human:** Ophir (communicates via Signal messenger)

## Key Differences from R3 (nuc-orchestrator)

1. **All compute on NUC** — Vector's CPU is too weak for inference
2. **gRPC over WiFi** — replaces SSH + HTTP bridge + ROS2
3. **No Docker on Vector** — no containers, no ROS2, thin client only
4. **Differential drive** — no strafing, turn-then-drive planner
5. **No LiDAR** — camera-only SLAM, cliff sensors for safety
6. **wire-pod on NUC** — replaces Anki cloud services
7. **New hardware** — face display, lift, cube, touch sensor, 4-mic beamforming

## Voice Pipeline

```
Vector mic → wake word (Porcupine PV) → wire-pod (Vosk STT)
  → IntentGraph → openclaw-voice-proxy → OpenClaw LLM → Vector SayText
```

- **wire-pod** (`wire-pod.service`): Replaces Anki cloud. Handles wake word, STT (Vosk), intent routing.
- **Voice proxy** (`scripts/openclaw-voice-proxy.py`): Standalone script that bridges wire-pod to OpenClaw via OpenAI-compatible API. Serializes requests to prevent double-trigger abort. 60s timeout for tool-heavy queries. Not deployed as a systemd service.
- **Built-in intents disabled**: All wire-pod intents set to `requiresexact=True` in `en-US.json` so conversational queries route to OpenClaw instead of being intercepted.
- **Quiet mode**: Bridge releases SDK behavior control and sends `intent_imperative_quiet` via wire-pod's `cloud_intent` API to activate Vector's built-in QuietMode behavior. Vector sits still with head down but TriggerWordDetected stays active — wake word works, voice interactions work. A keepalive thread re-sends the quiet intent every 15s because voice interactions deactivate the intent. Switch to playful mode via `POST /mode {"mode": "playful"}` to let Vector roam freely.
- **Voice context**: Voice proxy prepends context to every message telling OpenClaw the speaker is Ophir, so commands like "send to Ophir hello" work without OpenClaw asking who the recipient is.
- **Firmware**: WireOS 3.0.1.32oskr (slot B). Stock 2.0.1.6091oskr on slot A as fallback.

## Key Files to Read First

1. **`REPO_MAP.md`** — monorepo directory structure and entry points
2. **`.claude/CLAUDE.md`** — architecture, agent definitions, safety rules
3. **`docs/vector/oskr-research.md`** — OSKR SDK research & feature portability from R3
