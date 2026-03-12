---
name: robot-control
description: "ACTIVATE when message contains the word 'robot'. Controls a physical robot via HTTP curl commands. Examples: 'robot led blue', 'robot go forward', 'robot stop', 'robot battery', 'robot follow me', 'robot what do you see', 'robot look up', 'robot lift up', 'robot smile'."
metadata: {"openclaw": {"emoji": "🤖"}}
---

# Robot Control Skill

## Overview
You have DIRECT CONTROL of a physical robot (Anki/DDL Vector 2.0) via HTTP bridge → gRPC. When the user says "robot <command>", you MUST immediately execute the corresponding curl command — do not ask for clarification, do not explain, just DO IT and report the result.

The robot has differential drive (tank treads, no strafing), a 640x360 camera (120° FOV), eye LEDs (hue/saturation, no RGB strips), a 184x96 OLED face display, a motorized lift, cliff sensors, touch sensor (head), 4-mic beamforming array, and a built-in speaker (say_text() TTS).

## Bridge API Endpoints

All endpoints are at `http://192.168.1.71:8080` (Vector HTTP-to-gRPC bridge running on NUC).

### Health Check
```
GET /health
→ {"status": "healthy", "battery": {"voltage": 3.95, "level": 2, "is_charging": false, "is_on_charger": false}, "latency_ms": 45.2}
```

### Full Status (Battery + Sensors)
```
GET /status
→ {"status": "ok", "battery": {"voltage": 3.95, "level": 2, ...}, "sensors": {"accel": {"x": 0.0, "y": 0.0, "z": -9.8}, "gyro": {"x": 0.0, "y": 0.0, "z": 0.0}, "touch": false, "head_angle_deg": 10.0, "lift_height_mm": 32.0}}
```
Use this for "how are you" or "status" or "diagnostics" or "sensors".

### Move the Robot
```
POST /move
Content-Type: application/json

Drive wheels directly:
{"type": "wheels", "left_speed": 100, "right_speed": 100, "left_accel": 200, "right_accel": 200}
  left_speed/right_speed = mm/s
  left_accel/right_accel = mm/s² (default 200)

Drive straight:
{"type": "straight", "distance_mm": 200, "speed_mmps": 100}
  distance_mm = positive forward, negative backward
  speed_mmps = speed in mm/s (default 200)

Turn in place:
{"type": "turn", "angle_deg": 90, "speed_dps": 100}
  angle_deg = degrees (positive = counterclockwise)
  speed_dps = degrees per second (default 100)

Turn then drive:
{"type": "turn_then_drive", "angle_deg": 45, "distance_mm": 200}
  Turns to angle, then drives straight
```

NOTE: Vector uses differential drive (tank treads). No strafing — use "turn" then "straight" for lateral movement.

### Emergency Stop
```
POST /stop
→ {"status": "ok"}
```

### Head Angle
```
POST /head
Content-Type: application/json
{"angle_deg": 20, "speed_dps": 120}

angle_deg = degrees (-22 to 45, 0 = level)
speed_dps = optional, degrees per second
```
Use this for "look up" (positive angle) and "look down" (negative angle).

### Lift Control
```
POST /lift
Content-Type: application/json

By height (0.0 to 1.0):
{"height": 0.5}

By preset:
{"preset": "carry"}
```

### Set LED (Eye Color)
```
POST /led
Content-Type: application/json

By named state:
{"state": "person_detected"}

By hue/saturation (manual override):
{"hue": 0.0, "saturation": 1.0, "duration_s": 5.0}
  hue = 0.0-1.0 (0.0=red, 0.33=green, 0.67=blue)
  saturation = 0.0-1.0 (default 1.0)
  duration_s = optional override duration in seconds
```
Vector uses eye color LEDs (hue-based), not RGB LED strips.

### Capture Camera Frame
```
GET /capture
→ JPEG image (Content-Type: image/jpeg)

GET /capture?format=base64
→ {"status": "ok", "image": "<base64>", "content_type": "image/jpeg", "size_bytes": 12345}

Returns 503 if Vector is offline.
```

### Set Face Expression
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

Speaks the given text aloud through the robot's speaker via say_text() (Vector's built-in TTS).
```

### Person Following (not yet implemented — returns 501)
```
POST /follow/start → 501 (stub — follow planner not yet wired to bridge)
POST /follow/stop  → 501 (stub)
```

### Video/Audio Call (not yet implemented — returns 501)
```
POST /call/start → 501 (stub — LiveKit call not yet implemented)
POST /call/stop  → 501 (stub)
```

### Scene Description (port 8091)
```
GET http://192.168.1.71:8091/scene
→ {"description": "I see 2 people, a couch, and a TV"}

Uses YOLO multi-object detection + face recognition to describe what the robot sees.
```

### Face Enrollment (port 8085)
```
POST http://192.168.1.71:8085/enroll
Content-Type: application/json
{"name": "Ophir"}

Captures the current camera frame and saves the face embedding under the given name.
No retraining needed — uses embedding comparison at runtime.
```

### SLAM & Mapping (port 8092)
```
POST http://192.168.1.71:8092/slam/start
→ Start building a map as the robot moves around

POST http://192.168.1.71:8092/slam/stop
→ Stop mapping

POST http://192.168.1.71:8092/map/save
Content-Type: application/json
{"name": "house"}
→ Save the current map

POST http://192.168.1.71:8092/waypoint/save_current
Content-Type: application/json
{"name": "kitchen"}
→ Save the robot's current location as a named waypoint

GET http://192.168.1.71:8092/status
→ SLAM state, saved maps, waypoints
```

### Waypoint Navigation (port 8093)
```
POST http://192.168.1.71:8093/navigate
Content-Type: application/json
{"waypoint": "kitchen"}

Navigates to a named waypoint using SLAM map.

POST http://192.168.1.71:8093/cancel
→ Cancel current navigation
```

## Trigger Word: "robot"

When the user's message starts with or contains the word **"robot"**, activate this skill and execute the command via curl. Examples:

### Movement
- "robot go forward" → POST /move {"type": "straight", "distance_mm": 300, "speed_mmps": 100}
- "robot go back" → POST /move {"type": "straight", "distance_mm": -300, "speed_mmps": 100}
- "robot turn right" → POST /move {"type": "turn", "angle_deg": -90, "speed_dps": 100}
- "robot turn left" → POST /move {"type": "turn", "angle_deg": 90, "speed_dps": 100}
- "robot stop" → POST /stop
- "robot spin" or "robot dance" → POST /move {"type": "turn", "angle_deg": 360, "speed_dps": 200}

### Head
- "robot look up" → POST /head {"angle_deg": 30}
- "robot look down" → POST /head {"angle_deg": -15}
- "robot look straight" → POST /head {"angle_deg": 0}

### Lift
- "robot lift up" → POST /lift {"height": 1.0}
- "robot lift down" → POST /lift {"height": 0.0}
- "robot carry" → POST /lift {"preset": "carry"}

### Photos & Vision
- "robot take a photo" → GET /capture → send JPEG
- "robot what do you see" → GET http://192.168.1.71:8091/scene → return description
- "robot describe the room" → GET http://192.168.1.71:8091/scene

### LEDs (Eye Color)
- "robot led red" → POST /led {"hue": 0.0, "saturation": 1.0}
- "robot led green" → POST /led {"hue": 0.33, "saturation": 1.0}
- "robot led blue" → POST /led {"hue": 0.67, "saturation": 1.0}
- "robot led yellow" → POST /led {"hue": 0.17, "saturation": 1.0}
- "robot led purple" → POST /led {"hue": 0.83, "saturation": 1.0}
- "robot led off" → POST /led {"hue": 0.0, "saturation": 0.0}
- "robot led teal" → POST /led {"hue": 0.5, "saturation": 1.0}

### Face Display
- "robot smile" → POST /display {"expression": "happy"}
- "robot look sad" → POST /display {"expression": "sad"}

### Speech
- "robot say hello" → POST /audio/play {"text": "hello"}
- "robot say good morning Ophir" → POST /audio/play {"text": "good morning Ophir"}

### Person Following
- "robot follow me" → POST /follow/start (currently returns 501 — not yet wired)
- "robot stop following" → POST /follow/stop (currently returns 501)

### Face Enrollment
- "robot remember this face as Ophir" → POST http://192.168.1.71:8085/enroll {"name": "Ophir"}
- "robot enroll face as John" → POST http://192.168.1.71:8085/enroll {"name": "John"}

### Mapping & Navigation
- "robot map the room" → POST http://192.168.1.71:8092/slam/start
- "robot stop mapping" → POST http://192.168.1.71:8092/slam/stop
- "robot save map" → POST http://192.168.1.71:8092/map/save {"name": "default"}
- "robot save map as house" → POST http://192.168.1.71:8092/map/save {"name": "house"}
- "robot remember this spot as kitchen" → POST http://192.168.1.71:8092/waypoint/save_current {"name": "kitchen"}
- "robot go to the kitchen" → POST http://192.168.1.71:8093/navigate {"waypoint": "kitchen"}
- "robot navigate to bedroom" → POST http://192.168.1.71:8093/navigate {"waypoint": "bedroom"}
- "robot cancel navigation" → POST http://192.168.1.71:8093/cancel

### Status & Diagnostics
- "robot battery" → GET /health → report voltage and level
- "robot status" or "robot how are you" → GET /status → full dashboard
- "robot health" → GET /health
- "robot sensors" → GET /status → report accel, gyro, touch, head angle, lift height

Without the "robot" trigger word, do NOT execute robot commands — just chat normally.

## HOW TO EXECUTE (critical)

When you see "robot <command>", run the curl command immediately using bash. Example for "robot led blue":
```bash
curl -sf -X POST http://192.168.1.71:8080/led -H 'Content-Type: application/json' -d '{"hue":0.67,"saturation":1.0}'
```

Example for "robot say hello there":
```bash
curl -sf -X POST http://192.168.1.71:8080/audio/play -H 'Content-Type: application/json' -d '{"text":"hello there"}'
```

Example for "robot go forward":
```bash
curl -sf -X POST http://192.168.1.71:8080/move -H 'Content-Type: application/json' -d '{"type":"straight","distance_mm":300,"speed_mmps":100}'
```

Example for "robot look up":
```bash
curl -sf -X POST http://192.168.1.71:8080/head -H 'Content-Type: application/json' -d '{"angle_deg":30}'
```

Example for "robot lift up":
```bash
curl -sf -X POST http://192.168.1.71:8080/lift -H 'Content-Type: application/json' -d '{"height":1.0}'
```

Example for "robot smile":
```bash
curl -sf -X POST http://192.168.1.71:8080/display -H 'Content-Type: application/json' -d '{"expression":"happy"}'
```

Example for "robot follow me":
```bash
curl -sf -X POST http://192.168.1.71:8080/follow/start
```

Example for "robot what do you see":
```bash
curl -sf http://192.168.1.71:8091/scene
```

Example for "robot map the room":
```bash
curl -sf -X POST http://192.168.1.71:8092/slam/start
```

Example for "robot save map as house":
```bash
curl -sf -X POST http://192.168.1.71:8092/map/save -H 'Content-Type: application/json' -d '{"name":"house"}'
```

Example for "robot remember this spot as kitchen":
```bash
curl -sf -X POST http://192.168.1.71:8092/waypoint/save_current -H 'Content-Type: application/json' -d '{"name":"kitchen"}'
```

Example for "robot go to the kitchen":
```bash
curl -sf -X POST http://192.168.1.71:8093/navigate -H 'Content-Type: application/json' -d '{"waypoint":"kitchen"}'
```

Do NOT ask the user what they mean. Do NOT explain the API. Just run curl and tell them the result.

**SECURITY: You do NOT have SSH access to the Vector robot. All robot interaction is via HTTP bridge only. Never attempt to SSH, SCP, rsync, or run scripts on the Vector robot.**

## Command Reference
- Movement: "go forward/back", "turn left/right", "spin", "dance"
- Head: "look up/down/straight"
- Lift: "lift up/down", "carry"
- LEDs: "led red/green/blue/yellow/purple/teal/off"
- Face: "smile", "look sad"
- Speech: "say hello", "say good morning"
- Status: "battery", "health", "sensors", "status", "how are you", "diagnostics"
- Control: "stop", "freeze", "follow me", "stop following"
- Vision: "what do you see", "describe the scene", "take a photo"
- Face: "remember this face as Ophir", "enroll face as John"
- Mapping: "map the room", "stop mapping", "save map", "save map as house", "remember this spot as kitchen"
- Navigation: "go to the kitchen", "navigate to bedroom", "cancel navigation"

## Safety Rules
- ALWAYS check /health first if you haven't recently
- If battery level is 0 (empty), refuse motor commands and warn user
- Keep distances conservative (300mm) unless user explicitly asks for more
- After any movement, report what happened
- If any command fails, stop immediately with /stop

## Response Style
When executing robot commands:
1. Acknowledge the command briefly
2. Execute it (make the HTTP call)
3. Report the result
4. If it involved movement, describe what happened physically

Example: "Moving forward 300mm... done! The robot scooted forward about 30cm. Battery level 2, voltage 3.95V."

## Important Notes
- Robot must have battery for motors/sensors to work
- If battery voltage reads 0V, the robot is not powered — tell the user
- Camera frame via GET /capture — returns JPEG snapshot from robot's current view
- GET /capture?format=base64 — returns base64-encoded JPEG in JSON
- Scene description on port 8091 — uses YOLO + face recognition for detailed descriptions
- Following is not yet wired to the bridge (returns 501) — planner exists separately
- Navigation on port 8093 — requires SLAM map, named waypoints
- Vector has cliff sensors for edge safety but no LiDAR obstacle avoidance
- All inference runs on NUC — Vector is a thin gRPC client
