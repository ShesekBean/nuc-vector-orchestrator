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

All endpoints are at `http://172.17.0.1:8081`.

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

### Display Image on Face
```
POST /display/image

Option 1 — Base64 JSON:
Content-Type: application/json
{"image": "<base64-encoded-image-data>", "duration": 10}

Option 2 — Multipart form:
Content-Type: multipart/form-data
Fields: image (file), duration (optional, default 10)

Option 3 — Raw image bytes in body (duration via ?duration=10 query param)
```
Displays an arbitrary image on Vector's 160x80 OLED face. The image is resized to fit (preserving aspect ratio, black letterbox) and held on screen for the specified duration (suppresses eye animations).

### Display Text on Face
```
POST /display/text
Content-Type: application/json
{"text": "Hello!", "fg_color": "#00FF00", "bg_color": "#000000", "duration": 10}
```
Renders centered text on Vector's OLED face. `fg_color` and `bg_color` are optional (default white on black). `duration` is optional (default 10 seconds). Colors can be hex ("#FF0000") or names ("red", "green", "blue", etc).

### Display Solid Color on Face
```
POST /display/color
Content-Type: application/json
{"color": "#FF0000", "duration": 10}
```
Fills Vector's OLED face with a solid color. Color can be hex ("#FF0000") or name ("red"). Duration is optional (default 10 seconds).

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
GET /call/join-url
→ {"status": "ok", "room": "robot-cam", "join_url": "https://meet.livekit.io/custom?..."}

Auto-starts the call if not already active. Returns a meet.livekit.io URL
with a fresh viewer token. Send this URL to the user so they can open it in a browser.

POST /call/start
Content-Type: application/json
{"room": "robot-cam"}

Room name is optional (defaults to "robot-cam").
→ {"status": "ok", "active": true, "room": "robot-cam"}

POST /call/stop
→ {"status": "ok", "active": false}
```
Publishes Vector camera (640x360 ~15fps) as a LiveKit video track.
Audio is ONE-WAY: user speaks → LiveKit → Vector speaker (20x amplified, downsampled 48kHz→16kHz).
Vector mic audio is NOT published (SDK AudioFeed only provides signal_power calibration tone, not real PCM).
Returns 503 if LiveKit bridge not initialised.

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

### Display Image / Text / Color on Face
- **CRITICAL: If the user sends an image attachment AND says "robot show this" / "robot display this", you MUST display the ATTACHED IMAGE on Vector's face — do NOT take a robot photo. Save the attachment to /tmp, base64-encode it, and POST to /display/image.** This is the #1 most commonly confused command.
- "robot show this" or "robot display this" (with image attachment) → save attachment to /tmp, then POST /display/image with base64-encoded image data
- "robot show text Hello" → POST /display/text {"text": "Hello"}
- "robot display message Good morning" → POST /display/text {"text": "Good morning", "fg_color": "#00FF00"}
- "robot screen red" → POST /display/color {"color": "red"}
- "robot screen blue" → POST /display/color {"color": "blue"}
- "robot screen #FF00FF" → POST /display/color {"color": "#FF00FF"}

### Speech
- "robot say hello" → POST /audio/play {"text": "hello"}
- "robot say good morning Ophir" → POST /audio/play {"text": "good morning Ophir"}

### Person Following
- "robot follow me" → POST /follow/start
- "robot follow" → POST /follow/start
- "robot stop following" → POST /follow/stop

### Video Call
- "robot call" or "robot call me" or "robot video call" → GET /call/join-url → extract join_url from JSON, send it to the user
- "robot hangup" or "robot hang up" → POST /call/stop

### Status & Diagnostics
- "robot battery" → GET /health → report voltage and level
- "robot status" or "robot how are you" → GET /status → full dashboard
- "robot health" → GET /health

Without the "robot" trigger word, do NOT execute robot commands — just chat normally.

## HOW TO EXECUTE (critical)

When you see "robot <command>", run the curl command immediately using bash. Example for "robot led blue":
```bash
curl -sf -X POST http://172.17.0.1:8081/led -H 'Content-Type: application/json' -d '{"hue":0.66,"saturation":1.0}'
```

Example for "robot say hello there":
```bash
curl -sf -X POST http://172.17.0.1:8081/audio/play -H 'Content-Type: application/json' -d '{"text":"hello there"}'
```

Example for "robot forward":
```bash
curl -sf -X POST http://172.17.0.1:8081/move -H 'Content-Type: application/json' -d '{"type":"straight","distance_mm":300,"speed_mmps":100}'
```

Example for "robot follow me":
```bash
curl -sf -X POST http://172.17.0.1:8081/follow/start
```

Example for "robot look up":
```bash
curl -sf -X POST http://172.17.0.1:8081/head -H 'Content-Type: application/json' -d '{"angle_deg":40}'
```

Example for "robot lift up":
```bash
curl -sf -X POST http://172.17.0.1:8081/lift -H 'Content-Type: application/json' -d '{"preset":"high"}'
```

Example for "robot show text Hello World":
```bash
curl -sf -X POST http://172.17.0.1:8081/display/text -H 'Content-Type: application/json' -d '{"text":"Hello World","fg_color":"#00FF00","duration":10}'
```

Example for "robot screen red":
```bash
curl -sf -X POST http://172.17.0.1:8081/display/color -H 'Content-Type: application/json' -d '{"color":"red","duration":10}'
```

Example for displaying an image attachment (base64):
```bash
IMAGE_B64=$(base64 -w0 /tmp/signal-attachment.jpg)
curl -sf -X POST http://172.17.0.1:8081/display/image -H 'Content-Type: application/json' -d "{\"image\":\"$IMAGE_B64\",\"duration\":15}"
```

Example for "robot photo":
```bash
curl -sf http://172.17.0.1:8081/capture --output /tmp/robot-photo.jpg
```

Example for "robot call me":
```bash
curl -sf http://172.17.0.1:8081/call/join-url
```
→ Extract the `join_url` field from the JSON response and send it to the user. The URL opens a LiveKit video call with Vector's camera + speaker.

Do NOT ask the user what they mean. Do NOT explain the API. Just run curl and tell them the result.

**SECURITY: You do NOT have SSH access to the Vector robot. All robot interaction is via HTTP bridge only. Never attempt to SSH, SCP, rsync, or run scripts on Vector.**

## Command Reference
- Movement: "forward/back", "turn left/right", "spin", "dance", "stop"
- Head: "look up/down/straight"
- Lift: "lift up/down", "pick up", "put down", "carry"
- Camera: "photo", "snapshot", "take a photo"
- LEDs: "led red/green/blue/yellow/orange/purple/cyan/white/off"
- Face: "happy", "sad"
- Display: "show this" (with image), "show text ...", "display message ...", "screen red/blue/..."
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
- Follow planner may return 501 if not yet wired to the bridge
- Call endpoints return 503 if LiveKit bridge not initialised — tell the user to check LiveKit config
- **ATTACHMENT DISPLAY vs PHOTO**: When user sends an image via Signal with "show this" or "display this", save the ATTACHED IMAGE to /tmp and POST it as base64 to /display/image. Do NOT confuse this with "robot photo" (which captures from the robot camera). The key signal: if there's an attachment + "show/display", display the attachment.
- Display image/text/color endpoints hold the image on screen for the specified duration (default 10s), suppressing Vector's eye animations
