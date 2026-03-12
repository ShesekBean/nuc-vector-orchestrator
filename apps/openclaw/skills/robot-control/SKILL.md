---
name: robot-control
description: "ACTIVATE when message contains the word 'robot'. Controls a physical robot via HTTP curl commands. Examples: 'robot forward', 'robot stop', 'robot led blue', 'robot follow me', 'robot photo', 'robot status', 'robot lift up', 'robot say hello', 'robot call me'."
metadata: {"openclaw": {"emoji": "🤖"}}
---

# Robot Control Skill

## Overview
You have DIRECT CONTROL of a physical robot (Anki/DDL Vector 2.0) via HTTP bridge on the NUC. When the user says "robot <command>", you MUST immediately execute the corresponding curl command — do not ask for clarification, do not explain, just DO IT and report the result.

The robot has differential drive (tank treads, NO strafing), a 640x360 camera (120° FOV), eye color LEDs (hue/saturation), a 184x96 OLED face display, a lift mechanism, cliff sensors, touch sensor (head), 4-mic beamforming array, and a built-in speaker (say_text() TTS).

## Bridge API Endpoints

All endpoints are at `http://localhost:8080`.

### Health Check
```
GET /health
→ {"status": "healthy", "battery": {"voltage": 3.7, "level": 2, "is_charging": false, "is_on_charger": false}, "latency_ms": 45.2}
```

### Full Status
```
GET /status
→ {"status": "ok", "battery": {...}, "sensors": {"accel": {x,y,z}, "gyro": {x,y,z}, "touch": true/false, "head_angle_deg": 10.0, "lift_height_mm": 32.0}}
```
Use this for "how are you", "status", or "diagnostics".

### Move the Robot
```
POST /move
Content-Type: application/json

Drive wheels directly:
{"type": "wheels", "left_speed": 100, "right_speed": 100}
Speed in mm/s. Use matching speeds for straight, opposite for spin.

Drive straight a distance:
{"type": "straight", "distance_mm": 200, "speed_mmps": 100}
Positive = forward, negative = backward.

Turn in place:
{"type": "turn", "angle_deg": 90, "speed_dps": 100}
Positive = left (counterclockwise), negative = right (clockwise).

Turn then drive:
{"type": "turn_then_drive", "angle_deg": 45, "distance_mm": 200}
Turns first, then drives straight.
```

**IMPORTANT: Vector has differential drive (tank treads). NO strafing/sliding. To go sideways, turn first then drive forward.**

### Stop
```
POST /stop
→ {"status": "ok"}
```
Emergency stop — zeroes all motors immediately.

### Head Angle
```
POST /head
Content-Type: application/json
{"angle_deg": 10}

angle_deg: -22 (looking down) to 45 (looking up). Default neutral ~10.
Optional: "speed_dps": 120
```

### Lift
```
POST /lift
Content-Type: application/json

By height (0.0 = fully down, 1.0 = fully up):
{"height": 0.5}

By preset:
{"preset": "low"}     → fully down
{"preset": "carry"}   → middle carry position
{"preset": "high"}    → fully up
```

### LED (Eye Color)
```
POST /led
Content-Type: application/json

By named state:
{"state": "idle"}
{"state": "listening"}
{"state": "thinking"}
{"state": "person_detected"}
{"state": "error"}

By hue/saturation override:
{"hue": 0.0, "saturation": 1.0}
{"hue": 0.6, "saturation": 1.0, "duration_s": 5.0}

Hue values (0.0–1.0): 0.0=red, 0.08=orange, 0.16=yellow, 0.33=green, 0.5=cyan, 0.66=blue, 0.83=purple, 1.0=red again
Saturation: 0.0=white, 1.0=fully saturated
duration_s: optional, auto-reverts after N seconds
```

### Capture Camera Frame
```
GET /capture
→ JPEG image (Content-Type: image/jpeg)

GET /capture?format=base64
→ {"status": "ok", "image": "<base64>", "content_type": "image/jpeg", "size_bytes": 12345}
```

### Display (Face Expression)
```
POST /display
Content-Type: application/json
{"expression": "happy"}
```

### Speak Text (TTS)
```
POST /audio/play
Content-Type: application/json
{"text": "hello there"}

Speaks the given text aloud through Vector's built-in speaker via say_text().
```

### Person Following
```
POST /follow/start
→ Start following the closest person

POST /follow/stop
→ Stop following
```
Note: Follow planner may return 501 if not yet wired to the bridge.

### Video/Audio Call (LiveKit)
```
POST /call/start → Start a LiveKit video/audio call
POST /call/stop  → End the call
```
Note: Call endpoints may return 501 if not yet wired.

## Trigger Word: "robot"

When the user's message starts with or contains the word **"robot"**, activate this skill and execute the command via curl. Examples:

### Movement
- "robot forward" or "robot go forward" → POST /move {"type": "straight", "distance_mm": 300, "speed_mmps": 100}
- "robot back" or "robot go back" → POST /move {"type": "straight", "distance_mm": -300, "speed_mmps": 100}
- "robot turn left" → POST /move {"type": "turn", "angle_deg": 90, "speed_dps": 100}
- "robot turn right" → POST /move {"type": "turn", "angle_deg": -90, "speed_dps": 100}
- "robot stop" → POST /stop
- "robot spin" or "robot dance" → POST /move {"type": "turn", "angle_deg": 360, "speed_dps": 200}

### Head (Camera Angle)
- "robot look up" → POST /head {"angle_deg": 40}
- "robot look down" → POST /head {"angle_deg": -20}
- "robot look straight" → POST /head {"angle_deg": 10}

### Lift
- "robot lift up" or "robot pick up" → POST /lift {"preset": "high"}
- "robot lift down" or "robot put down" → POST /lift {"preset": "low"}
- "robot carry" → POST /lift {"preset": "carry"}

### Photos & Vision
- "robot photo" or "robot take a photo" → GET /capture → send JPEG as Signal attachment
- "robot snapshot" → GET /capture

### LEDs (Eye Color)
- "robot led red" → POST /led {"hue": 0.0, "saturation": 1.0}
- "robot led orange" → POST /led {"hue": 0.08, "saturation": 1.0}
- "robot led yellow" → POST /led {"hue": 0.16, "saturation": 1.0}
- "robot led green" → POST /led {"hue": 0.33, "saturation": 1.0}
- "robot led cyan" → POST /led {"hue": 0.5, "saturation": 1.0}
- "robot led blue" → POST /led {"hue": 0.66, "saturation": 1.0}
- "robot led purple" → POST /led {"hue": 0.83, "saturation": 1.0}
- "robot led white" → POST /led {"hue": 0.0, "saturation": 0.0}
- "robot led off" → POST /led {"state": "idle"}

### Face Expression
- "robot happy" → POST /display {"expression": "happy"}
- "robot sad" → POST /display {"expression": "sad"}

### Speech
- "robot say hello" → POST /audio/play {"text": "hello"}
- "robot say good morning Ophir" → POST /audio/play {"text": "good morning Ophir"}

### Person Following
- "robot follow me" → POST /follow/start
- "robot follow" → POST /follow/start
- "robot stop following" → POST /follow/stop

### Video Call
- "robot call" or "robot call me" → POST /call/start → returns join URL, send to user
- "robot hangup" or "robot hang up" → POST /call/stop

### Status & Diagnostics
- "robot battery" → GET /health → report voltage and level
- "robot status" or "robot how are you" → GET /status → full dashboard
- "robot health" → GET /health

Without the "robot" trigger word, do NOT execute robot commands — just chat normally.

## HOW TO EXECUTE (critical)

When you see "robot <command>", run the curl command immediately using bash. Example for "robot led blue":
```bash
curl -sf -X POST http://localhost:8080/led -H 'Content-Type: application/json' -d '{"hue":0.66,"saturation":1.0}'
```

Example for "robot say hello there":
```bash
curl -sf -X POST http://localhost:8080/audio/play -H 'Content-Type: application/json' -d '{"text":"hello there"}'
```

Example for "robot forward":
```bash
curl -sf -X POST http://localhost:8080/move -H 'Content-Type: application/json' -d '{"type":"straight","distance_mm":300,"speed_mmps":100}'
```

Example for "robot follow me":
```bash
curl -sf -X POST http://localhost:8080/follow/start
```

Example for "robot look up":
```bash
curl -sf -X POST http://localhost:8080/head -H 'Content-Type: application/json' -d '{"angle_deg":40}'
```

Example for "robot lift up":
```bash
curl -sf -X POST http://localhost:8080/lift -H 'Content-Type: application/json' -d '{"preset":"high"}'
```

Example for "robot photo":
```bash
curl -sf http://localhost:8080/capture --output /tmp/robot-photo.jpg
```

Do NOT ask the user what they mean. Do NOT explain the API. Just run curl and tell them the result.

**SECURITY: You do NOT have SSH access to the Vector robot. All robot interaction is via HTTP bridge only. Never attempt to SSH, SCP, rsync, or run scripts on Vector.**

## Command Reference
- Movement: "forward/back", "turn left/right", "spin", "dance", "stop"
- Head: "look up/down/straight"
- Lift: "lift up/down", "pick up", "put down", "carry"
- Camera: "photo", "snapshot", "take a photo"
- LEDs: "led red/green/blue/yellow/orange/purple/cyan/white/off"
- Face: "happy", "sad"
- Speech: "say hello", "say good morning"
- Status: "battery", "health", "status", "how are you"
- Control: "stop", "freeze", "follow me", "stop following"
- Calls: "call me", "hang up"

## Safety Rules
- ALWAYS check /health first if you haven't recently
- If battery level is 0 (empty), refuse motor commands and warn user
- If is_on_charger is true, refuse motor commands (charger locks out movement)
- Keep distances conservative (200-300mm) unless user explicitly asks for more
- After any movement, report what happened
- If any command fails, stop immediately with /stop

## Response Style
When executing robot commands:
1. Acknowledge the command briefly
2. Execute it (make the HTTP call)
3. Report the result
4. If it involved movement, describe what happened physically

Example: "Driving forward 300mm... done! Vector scooted forward about 30cm. Battery at 3.7V (level 2)."

## Important Notes
- Vector uses differential drive (tank treads) — NO strafing/sliding sideways
- Head angle range: -22° (down) to 45° (up)
- LEDs are eye color only (hue + saturation), not RGB strips
- Camera frame via GET /capture — returns JPEG snapshot
- say_text() TTS via POST /audio/play — Vector's built-in speaker
- Lift has three presets: low, carry, high
- If bridge returns 503, Vector is offline — tell the user
- Follow and call endpoints may return 501 (not yet wired) — tell the user they're coming soon
