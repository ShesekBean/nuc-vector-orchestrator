# Project Vector (Vector Edition) — Summary

**Generated:** 2026-03-10
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

## Key Files to Read First

1. **`REPO_MAP.md`** — monorepo directory structure and entry points
2. **`.claude/CLAUDE.md`** — architecture, agent definitions, safety rules
3. **`docs/vector/oskr-research.md`** — OSKR SDK research & feature portability from R3
