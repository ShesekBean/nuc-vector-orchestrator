---
name: robot-control
description: "ACTIVATE when message contains the word 'robot'. Controls a physical robot via HTTP curl commands. Examples: 'robot led blue', 'robot go forward', 'robot stop', 'robot battery', 'robot follow me', 'robot what do you see', 'robot patrol', 'robot call me', 'robot map the room', 'robot go to the kitchen'."
metadata: {"openclaw": {"emoji": "🤖"}}
---

# Robot Control Skill

## Overview
You have DIRECT CONTROL of a physical robot (Anki/DDL Vector 2.0) via HTTP bridge → gRPC. When the user says "robot <command>", you MUST immediately execute the corresponding curl command — do not ask for clarification, do not explain, just DO IT and report the result.

The robot has differential drive (tank treads, no strafing), a 640x360 camera (120° FOV), backpack RGB LEDs, a 184x96 OLED face display, a lift mechanism, cliff sensors, touch sensor (head), 4-mic beamforming array, and a built-in speaker (say_text() TTS).

## Bridge API Endpoints

All endpoints are at `http://192.168.1.71:8081` unless noted otherwise.

### Read Sensors
```
GET /health
→ {"status": "ok", "battery_v": 12.1}

GET /sensors
→ {"accelerometer": [x,y,z], "gyroscope": [x,y,z], "attitude": [roll,pitch,yaw], "battery_v": 12.1}
```

### Full Status Dashboard
```
GET /status
→ Battery, ROS2 nodes, audio pipeline, detection, planner, temps, memory, uptime
```
Returns a comprehensive JSON with all subsystems. Use this for "how are you" or "status" or "diagnostics".

### Move the Robot
```
POST /move
Content-Type: application/json
{"vx": 0.2, "vy": 0.0, "vz": 0.0, "duration": 1.0}

vx = forward/backward (-0.5 to 0.5 m/s)
vy = left/right strafe (-0.5 to 0.5 m/s)
vz = rotation (-0.5 to 0.5 rad/s)
duration = seconds (max 5.0)
```

Safety: speed is clamped to ±0.5. Acceleration is limited. Collision guard stops if obstacle < 0.2m. Watchdog stops motors if no command in 1 second.

### Stop
```
POST /stop
→ {"status": "stopped"}
```

### Move Camera Servo
```
POST /servo
Content-Type: application/json
{"channel": 3, "angle": 90}

channel: 3 = yaw/pan (neutral 102), 4 = pitch/tilt (neutral 68)
angle: 0-180
```

### Set LED Color
```
POST /led
Content-Type: application/json
{"r": 255, "g": 0, "b": 0}

Sets all LED strips to the given color (0-255 each).
Named colors: red, green, blue, yellow, orange, purple, white, cyan, pink, off
```

### Set LED Effect
```
POST /led/effect
Content-Type: application/json
{"effect": 0, "speed": 50, "parm": 0}

Effects: 0=blink, 1=fade/breathe, 2=running/chase, 3=steady
Speed: 0-100
```

### Beep
```
POST /beep
Content-Type: application/json
{"duration": 200}

Duration in milliseconds (max 2000).
```

### Capture Camera Frame
```
GET /capture
→ JPEG image (Content-Type: image/jpeg)
Returns 503 if detector is not running.
```

### Speak Text (TTS)
```
POST /say
Content-Type: application/json
{"text": "hello there"}

Speaks the given text aloud through the robot's speakers via say_text() (Vector's built-in TTS).
Text is capped at 400 characters.
```

### Set Individual Motors
```
POST /motor
Content-Type: application/json
{"speeds": [50, 50, 50, 50]}

Individual motor speeds (-50 to 50). Use /move instead for coordinated motion.
```

### Person Following
```
POST /follow/start
→ Start following the closest person

POST /follow/start
Content-Type: application/json
{"target": "Ophir"}
→ Follow a specific named person (requires face enrollment first)

POST /follow/stop
→ Stop following

GET /follow/status
→ Current follow state
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
→ SLAM state, nav2 status, saved maps, waypoints
```

### Waypoint Navigation (port 8093)
```
POST http://192.168.1.71:8093/navigate
Content-Type: application/json
{"waypoint": "kitchen"}

Navigates to a named waypoint using nav2 + SLAM map.

POST http://192.168.1.71:8093/cancel
→ Cancel current navigation
```

### Patrol
```
POST /patrol/start   → Start autonomous patrol loop
POST /patrol/stop    → Stop patrol
POST /patrol/pause   → Pause patrol (can resume)
POST /patrol/resume  → Resume paused patrol
GET  /patrol/status  → Current patrol state, waypoint, loop count, detections
```

### Video/Audio Call
```
POST /call/start → Start a LiveKit video/audio call (returns join URL)
POST /call/stop  → End the call
```

### Camera Feed
```
GET /camera → Returns LiveKit camera feed URL for live viewing
```

### Manual Mode
```
POST /manual/on  Content-Type: application/json {"duration": 60}
→ Pauses autonomous planner + servo tracking for N seconds

POST /manual/off
→ Restores autonomous mode
```

## Trigger Word: "robot"

When the user's message starts with or contains the word **"robot"**, activate this skill and execute the command via curl. Examples:

### Movement
- "robot go forward" → POST /move {"vx": 0.3, "duration": 1.5}
- "robot go back" → POST /move {"vx": -0.3, "duration": 1.5}
- "robot slide left" → POST /move {"vy": 0.3, "duration": 1.5}
- "robot turn right" → POST /move {"vz": -0.3, "duration": 1.5}
- "robot stop" → POST /stop
- "robot spin" or "robot dance" → POST /move {"vz": 0.4, "duration": 2.0}

### Camera
- "robot look left" → POST /servo {"channel": 3, "angle": 140}
- "robot look right" → POST /servo {"channel": 3, "angle": 50}
- "robot look up" → POST /servo {"channel": 4, "angle": 50}
- "robot look down" → POST /servo {"channel": 4, "angle": 110}
- "robot center camera" → POST /servo {"channel": 3, "angle": 102} then {"channel": 4, "angle": 68}

### Photos & Vision
- "robot take a photo" → POST /intercom/photo {"caption": "Photo from robot"} → sends photo to Ophir's Signal DM
- "robot what do you see" → GET http://192.168.1.71:8091/scene → return description
- "robot describe the room" → GET http://192.168.1.71:8091/scene

### LEDs
- "robot led red" → POST /led {"r": 255, "g": 0, "b": 0}
- "robot led off" → POST /led {"r": 0, "g": 0, "b": 0}
- "robot led blink" → POST /led/effect {"effect": 0, "speed": 50, "parm": 0}
- "robot led fade" → POST /led/effect {"effect": 1, "speed": 50, "parm": 0}
- "robot led running" → POST /led/effect {"effect": 2, "speed": 50, "parm": 0}
- "robot led charging" → POST /led green + POST /led/effect running

### Speech (Intercom)
- "robot say hello" → POST /say {"text": "hello"}
- "robot say good morning Ophir" → POST /say {"text": "good morning Ophir"}
- "robot tell ophir hello" → POST /intercom/send {"text": "hello"} → sends text to Ophir's Signal DM
- "robot tell ophir I'm at the door" → POST /intercom/send {"text": "I'm at the door"}

### Person Following
- "robot follow me" → POST /follow/start
- "robot follow Ophir" → POST /follow/start {"target": "Ophir"}
- "robot stop following" → POST /follow/stop

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

### Patrol
- "robot patrol" → POST /patrol/start
- "robot stop patrol" → POST /patrol/stop
- "robot pause patrol" → POST /patrol/pause
- "robot resume patrol" → POST /patrol/resume
- "robot patrol status" → GET /patrol/status

### Video Call
- "robot call me" → POST /call/start → returns join URL, send to user
- "robot hang up" → POST /call/stop
- "robot camera" → GET /camera → returns live feed URL

### Status & Diagnostics
- "robot battery" → GET /health → report voltage and percentage
- "robot status" or "robot how are you" → GET /status → full dashboard
- "robot health" → GET /health
- "robot sensors" → GET /sensors

### Sound
- "robot beep" → POST /beep {"duration": 200}

Without the "robot" trigger word, do NOT execute robot commands — just chat normally.

## HOW TO EXECUTE (critical)

When you see "robot <command>", run the curl command immediately using bash. Example for "robot led blue":
```bash
curl -sf -X POST http://192.168.1.71:8081/led -H 'Content-Type: application/json' -d '{"r":0,"g":0,"b":255}'
```

Example for "robot say hello there":
```bash
curl -sf -X POST http://192.168.1.71:8081/say -H 'Content-Type: application/json' -d '{"text":"hello there"}'
```

Example for "robot follow me":
```bash
curl -sf -X POST http://192.168.1.71:8081/follow/start
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

Movement commands automatically pause the autonomous planner for 10 seconds. No extra steps needed.

**SECURITY: You do NOT have SSH access to the Jetson. All robot interaction is via HTTP bridge only. Never attempt to SSH, SCP, rsync, or run scripts on the Jetson.**

## Command Reference
- Movement: "go forward/back", "turn left/right", "slide left/right", "spin", "dance"
- Camera: "look up/down/left/right", "center camera", "take a photo", "snapshot", "what do you see", "describe the room", "camera"
- LEDs: "led red/green/blue/off", "led blink/fade/running/charging", "led rgb 255 0 0"
- Speech: "say hello", "say good morning"
- Sound: "beep", "honk"
- Status: "battery", "health", "sensors", "status", "how are you", "diagnostics"
- Control: "stop", "freeze", "follow me", "follow Ophir", "stop following"
- Vision: "what do you see", "describe the scene", "what's around"
- Face: "remember this face as Ophir", "enroll face as John"
- Mapping: "map the room", "stop mapping", "save map", "save map as house", "remember this spot as kitchen"
- Navigation: "go to the kitchen", "navigate to bedroom", "cancel navigation"
- Patrol: "patrol", "stop patrol", "pause patrol", "resume patrol", "patrol status"
- Calls: "call me", "hang up", "end call"

## Safety Rules
- ALWAYS check /health first if you haven't recently
- If battery < 10V, refuse motor commands and warn user
- Keep speeds conservative (0.3 m/s) unless user explicitly asks for faster
- Duration max 2 seconds unless user specifies longer
- After any movement, report what happened
- If any command fails, stop immediately with /stop

## Response Style
When executing robot commands:
1. Acknowledge the command briefly
2. Execute it (make the HTTP call)
3. Report the result
4. If it involved movement, describe what happened physically

Example: "Moving forward 0.3 m/s for 1.5 seconds... done! The robot scooted forward about 45cm. Battery at 11.8V."

## Important Notes
- Robot must have battery connected for motors/sensors to work
- If battery reads 0V, the robot is not powered — tell the user
- Camera frame via GET /capture — returns JPEG snapshot from robot's current view
- Scene description on port 8091 — uses YOLO + face recognition for detailed descriptions
- Following service on bridge (port 8081) — supports generic and named person following
- Navigation on port 8093 — requires SLAM map, named waypoints
- LiDAR data isn't exposed via the bridge API — collision guard works internally
- Charging mode locks out movement — bridge returns 409 if robot is charging
