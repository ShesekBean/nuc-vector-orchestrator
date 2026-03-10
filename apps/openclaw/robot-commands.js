// robot-commands.js — Parse Signal messages for robot movement intent, POST to Vector bridge
//
// This module is an ADDITIVE OpenClaw component.
// It intercepts robot-related Signal messages, parses movement commands,
// validates against the command allowlist, and forwards HTTP calls to the
// Vector bridge at http://192.168.1.71:8081.
//
// Usage:
//   const { parseCommand, executeCommand, handleMessage } = require('./robot-commands');
//   const response = await handleMessage("go forward");
//
// CLI:
//   node robot-commands.js "go forward"

const http = require("http");
const path = require("path");
const { execFile } = require("child_process");

const BRIDGE_HOST = process.env.BRIDGE_HOST || "192.168.1.71";
const BRIDGE_PORT = normalizePort(process.env.BRIDGE_PORT, 8081);
const FOLLOW_PORT = normalizePort(process.env.FOLLOW_PORT, 8084);
const SCENE_PORT = normalizePort(process.env.SCENE_PORT, 8091);
const NAV_PORT = normalizePort(process.env.NAV_PORT, 8093);
const ENROLL_PORT = normalizePort(process.env.ENROLL_PORT, 8085);
const ROBOT_COMMAND_TRANSPORT = normalizeTransportMode(process.env.ROBOT_COMMAND_TRANSPORT || "http");
const MQTT_BROKER_HOST = process.env.MQTT_BROKER_HOST || BRIDGE_HOST;
const MQTT_BROKER_PORT = normalizePort(process.env.MQTT_BROKER_PORT, 1883);
const MQTT_TOPIC_PREFIX = sanitizeTopicPrefix(process.env.MQTT_TOPIC_PREFIX || "robot/commands");
const MQTT_QOS = normalizeMqttQos(process.env.MQTT_QOS || "1");
const MQTT_TIMEOUT_MS = normalizeTimeoutMs(process.env.MQTT_TIMEOUT_MS, 5000);
const MQTT_PUBLISH_CMD = process.env.MQTT_PUBLISH_CMD || "mosquitto_pub";

const DEFAULT_SPEED = 0.3;
const DEFAULT_DURATION = 1.5;

// Servo angle presets for "look" commands (ch3=yaw, ch4=pitch)
const SERVO_PRESETS = {
  left:   { channel: 3, angle: 140 },
  right:  { channel: 3, angle: 50 },
  up:     { channel: 4, angle: 50 },
  down:   { channel: 4, angle: 110 },
};
const SERVO_YAW_NEUTRAL = 96;
const SERVO_PITCH_NEUTRAL = 78;
const PHOTO_SAVE_DIR = process.env.PHOTO_SAVE_DIR || "/tmp";
const SIGNAL_GROUP_ID = process.env.SIGNAL_GROUP_ID || "BUrA+nRRpsfdYgftby/jpJ7Ugy5PBzYWg89oNNr4nF4=";
const BOT_CONTAINER = process.env.BOT_CONTAINER || "openclaw-gateway";

// Named color → RGB mapping for LED commands
const COLOR_MAP = {
  red:    { r: 255, g: 0,   b: 0   },
  green:  { r: 0,   g: 255, b: 0   },
  blue:   { r: 0,   g: 0,   b: 255 },
  yellow: { r: 255, g: 255, b: 0   },
  orange: { r: 255, g: 165, b: 0   },
  purple: { r: 128, g: 0,   b: 128 },
  white:  { r: 255, g: 255, b: 255 },
  cyan:   { r: 0,   g: 255, b: 255 },
  pink:   { r: 255, g: 105, b: 180 },
  off:    { r: 0,   g: 0,   b: 0   },
};

// LED effect name → effect ID mapping (matches Rosmaster_Lib set_colorful_effect)
const EFFECT_MAP = {
  blink:   { effect: 0, speed: 50, parm: 0 },
  fade:    { effect: 1, speed: 50, parm: 0 },
  breathe: { effect: 1, speed: 50, parm: 0 },
  running: { effect: 2, speed: 50, parm: 0 },
  steady:  { effect: 3, speed: 0,  parm: 0 },
};

/**
 * Parse an LED color command with named color or RGB values.
 * Returns { r, g, b } or null if not recognized.
 */
function parseLedColor(text) {
  // Try named color (skip prepositions like "to")
  const colorMatch = text.match(/(?:led|light|lights|color)\s+(?:to\s+)?(\w+)/i);
  if (colorMatch) {
    const name = colorMatch[1].toLowerCase();
    if (COLOR_MAP[name]) return COLOR_MAP[name];
  }
  // Try RGB: "led 255 0 0" or "led rgb 255 0 0"
  const rgbMatch = text.match(/(?:led|light|lights|color)\s+(?:rgb\s+)?(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})/i);
  if (rgbMatch) {
    const r = Math.min(255, parseInt(rgbMatch[1], 10));
    const g = Math.min(255, parseInt(rgbMatch[2], 10));
    const b = Math.min(255, parseInt(rgbMatch[3], 10));
    return { r, g, b };
  }
  return null;
}

/**
 * Parse an LED effect command.
 * Returns { effect, speed, parm } or null if not recognized.
 */
function parseLedEffect(text) {
  const effectMatch = text.match(/(?:led|light|lights)\s+(?:effect\s+)?(\w+)/i);
  if (effectMatch) {
    const name = effectMatch[1].toLowerCase();
    if (EFFECT_MAP[name]) return EFFECT_MAP[name];
  }
  return null;
}

// Command patterns: each entry maps regex patterns to bridge API parameters.
// Order matters — first match wins.
// Entries may use `sequence` (array of {endpoint, method, body}) for multi-step commands.
const COMMAND_MAP = [
  // Video/audio call — "call me" starts two-way LiveKit call, "hang up" ends it
  {
    patterns: [/\bcall\s+me\b/i, /\bvideo\s+call\b/i, /\bstart\s+call\b/i],
    endpoint: "/call/start",
    method: "POST",
    body: {},
    description: "start video call",
    isCall: true,
  },
  {
    patterns: [/\bhang\s+up\b/i, /\bend\s+call\b/i, /\bstop\s+call\b/i],
    endpoint: "/call/stop",
    method: "POST",
    body: {},
    description: "end video call",
  },
  // Intercom — "say <message>" speaks text aloud on robot speaker
  {
    patterns: [/\bsay\s+(.+)/i],
    endpoint: "/say",
    method: "POST",
    body: {},
    description: "speak aloud",
    isSay: true,
    dynamicBody: (text) => {
      const m = text.match(/\bsay\s+(.+)/i);
      return { text: m ? m[1].trim() : "" };
    },
  },
  // LED charging animation — green running/chase effect (before other LED entries)
  {
    patterns: [/\b(?:led|light|lights)\s+charging\b/i],
    sequence: [
      { endpoint: "/led", method: "POST", body: { r: 0, g: 255, b: 0 } },
      { endpoint: "/led/effect", method: "POST", body: { effect: 2, speed: 50, parm: 0 } },
    ],
    description: "charging LED indicator",
  },
  // LED effect commands (before color to catch "led blink red" etc.)
  {
    patterns: [/\b(?:led|light|lights)\s+(?:effect\s+)?(?:blink|fade|breathe|running|steady)\b/i],
    endpoint: "/led/effect",
    method: "POST",
    body: {},
    description: "set LED effect",
    dynamicBody: (text) => parseLedEffect(text) || { effect: 3, speed: 0, parm: 0 },
  },
  // LED color commands — named colors and RGB values
  {
    patterns: [
      /\b(?:led|light|lights|color)\s+(?:rgb\s+)?\d{1,3}\s+\d{1,3}\s+\d{1,3}\b/i,
      /\b(?:led|light|lights|color)\s+(?:to\s+)?(?:red|green|blue|yellow|orange|purple|white|cyan|pink|off)\b/i,
      /\b(?:set\s+)?(?:led|light|lights)\s+(?:to\s+)?(?:red|green|blue|yellow|orange|purple|white|cyan|pink|off)\b/i,
    ],
    endpoint: "/led",
    method: "POST",
    body: {},
    description: "set LED color",
    dynamicBody: (text) => parseLedColor(text) || { r: 255, g: 255, b: 255 },
  },
  // LED off shortcut for "turn off" phrasing
  {
    patterns: [/\bturn\s+off\s+(?:led|light|lights)\b/i],
    endpoint: "/led",
    method: "POST",
    body: { r: 0, g: 0, b: 0 },
    description: "turn off LEDs",
  },
  // Scene description — asks the robot what it sees
  {
    patterns: [
      /\bwhat\s+(?:do\s+you|can\s+you)\s+see\b/i,
      /\bdescribe\s+(?:the\s+)?(?:scene|surroundings|environment|room|view)\b/i,
      /\bwhat(?:'s| is)\s+(?:around|in front|ahead)\b/i,
      /\bscene\s+description\b/i,
    ],
    endpoint: "/scene",
    method: "GET",
    body: {},
    port: SCENE_PORT,
    description: "describe scene",
    isScene: true,
  },
  // Photo/capture commands — fetches image from bridge and sends via Signal
  {
    patterns: [
      /\btake\s+(?:a\s+)?(?:photo|picture|pic|snapshot|selfie)\b/i,
      /\b(?:capture|snap)\s+(?:an?\s+)?(?:photo|picture|pic|image|frame)\b/i,
      /\bphoto\b/i,
      /\bsnapshot\b/i,
    ],
    isPhoto: true,
    description: "take a photo",
  },
  // Face enrollment — remember a person's face
  {
    patterns: [
      /\bremember\s+(?:this\s+)?face\s+as\s+(\w+)/i,
      /\bremember\s+(?:me|this)\s+as\s+(\w+)/i,
      /\benroll\s+(?:face\s+)?(?:as\s+)?(\w+)/i,
    ],
    endpoint: "/enroll",
    method: "POST",
    body: {},
    port: ENROLL_PORT,
    description: "enroll face",
    dynamicBody: (text) => {
      const m = text.match(/\b(?:remember|enroll)\s+(?:this\s+)?(?:face\s+)?(?:me\s+)?(?:as\s+)?(\w+)/i);
      return { name: m ? m[1] : "unknown" };
    },
  },
  // Named following — "follow Ophir" (capitalized name, before generic follow)
  {
    patterns: [/\bfollow\s+([A-Z][a-z]+)\b/],
    endpoint: "/follow/start",
    method: "POST",
    body: {},
    port: FOLLOW_PORT,
    description: "follow person",
    dynamicBody: (text) => {
      const m = text.match(/\bfollow\s+([A-Z][a-z]+)\b/);
      return { target: m ? m[1] : undefined };
    },
  },
  {
    patterns: [/\bstop\s+follow/i],
    endpoint: "/follow/stop",
    method: "POST",
    body: {},
    port: FOLLOW_PORT,
    description: "stop following",
  },
  {
    patterns: [/\bfollow\s+status\b/i],
    endpoint: "/follow/status",
    method: "GET",
    body: {},
    port: FOLLOW_PORT,
    description: "follow status",
  },
  {
    patterns: [/\bfollow\s+me\b/i, /\bstart\s+follow/i, /\bfollow\b/i],
    endpoint: "/follow/start",
    method: "POST",
    body: {},
    port: FOLLOW_PORT,
    description: "follow me",
  },
  {
    patterns: [/\bstop\s+navigat/i, /\bcancel\s+navigat/i],
    endpoint: "/cancel",
    method: "POST",
    body: {},
    port: NAV_PORT,
    description: "cancel navigation",
  },
  // Patrol commands — stop/pause before generic stop
  {
    patterns: [/\b(?:stop|end|cancel)\s+patrol/i],
    endpoint: "/patrol/stop",
    method: "POST",
    body: {},
    description: "stop patrol",
    isPatrol: true,
  },
  {
    patterns: [/\bpause\s+patrol/i],
    endpoint: "/patrol/pause",
    method: "POST",
    body: {},
    description: "pause patrol",
    isPatrol: true,
  },
  {
    patterns: [/\b(?:resume|continue)\s+patrol/i],
    endpoint: "/patrol/resume",
    method: "POST",
    body: {},
    description: "resume patrol",
    isPatrol: true,
  },
  {
    patterns: [/\bpatrol\s+status\b/i],
    endpoint: "/patrol/status",
    method: "GET",
    body: {},
    description: "patrol status",
    isPatrol: true,
  },
  {
    patterns: [/\bstart\s+patrol/i, /\bbegin\s+patrol/i, /\bpatrol\b/i],
    endpoint: "/patrol/start",
    method: "POST",
    body: {},
    description: "start patrol",
    isPatrol: true,
  },
  {
    patterns: [/\bstop\b/i],
    endpoint: "/stop",
    method: "POST",
    body: {},
    description: "stop",
  },
  // Waypoint navigation — "go to the kitchen", "navigate to bedroom"
  {
    patterns: [
      /\bgo\s+to\s+(?:the\s+)?(\w+(?:\s+\w+)?)\b/i,
      /\bnavigate\s+to\s+(?:the\s+)?(\w+(?:\s+\w+)?)\b/i,
      /\bdrive\s+to\s+(?:the\s+)?(\w+(?:\s+\w+)?)\b/i,
    ],
    endpoint: "/navigate",
    method: "POST",
    body: {},
    port: NAV_PORT,
    description: "navigate to waypoint",
    isNavigation: true,
    dynamicBody: (text) => {
      const m = text.match(/\b(?:go|navigate|drive)\s+to\s+(?:the\s+)?(\w+(?:\s+\w+)?)\b/i);
      return { waypoint: m ? m[1].trim() : "unknown" };
    },
  },
  {
    patterns: [/\bgo\s+straight\b/i, /\bforward\b/i],
    endpoint: "/move",
    method: "POST",
    body: { vx: DEFAULT_SPEED, duration: DEFAULT_DURATION },
    description: "move forward",
  },
  {
    patterns: [/\bgo\s+back\b/i, /\bbackward\b/i],
    endpoint: "/move",
    method: "POST",
    body: { vx: -DEFAULT_SPEED, duration: DEFAULT_DURATION },
    description: "move backward",
  },
  {
    patterns: [/\bslide\s+left\b/i, /\bstrafe\s+left\b/i],
    endpoint: "/move",
    method: "POST",
    body: { vy: DEFAULT_SPEED, duration: DEFAULT_DURATION },
    description: "strafe left",
  },
  {
    patterns: [/\bslide\s+right\b/i, /\bstrafe\s+right\b/i],
    endpoint: "/move",
    method: "POST",
    body: { vy: -DEFAULT_SPEED, duration: DEFAULT_DURATION },
    description: "strafe right",
  },
  {
    patterns: [/\bturn\s+left\b/i],
    endpoint: "/move",
    method: "POST",
    body: { vz: DEFAULT_SPEED, duration: DEFAULT_DURATION },
    description: "turn left",
  },
  {
    patterns: [/\bturn\s+right\b/i],
    endpoint: "/move",
    method: "POST",
    body: { vz: -DEFAULT_SPEED, duration: DEFAULT_DURATION },
    description: "turn right",
  },
  // Battery status
  {
    patterns: [/\bbatter(?:y|ies)\b/i, /\bvoltage\b/i, /\bpower\s+(?:level|status)\b/i],
    endpoint: "/health",
    method: "GET",
    body: {},
    description: "check battery",
    isBattery: true,
  },
  // Spin / dance
  {
    patterns: [/\bspin\b/i, /\bdance\b/i],
    endpoint: "/move",
    method: "POST",
    body: { vz: 0.4, duration: 2.0 },
    description: "spin",
  },
  // Look direction commands — servo presets
  {
    patterns: [/\blook\s+left\b/i],
    endpoint: "/servo",
    method: "POST",
    body: SERVO_PRESETS.left,
    description: "look left",
  },
  {
    patterns: [/\blook\s+right\b/i],
    endpoint: "/servo",
    method: "POST",
    body: SERVO_PRESETS.right,
    description: "look right",
  },
  {
    patterns: [/\blook\s+up\b/i],
    endpoint: "/servo",
    method: "POST",
    body: SERVO_PRESETS.up,
    description: "look up",
  },
  {
    patterns: [/\blook\s+down\b/i],
    endpoint: "/servo",
    method: "POST",
    body: SERVO_PRESETS.down,
    description: "look down",
  },
  {
    patterns: [/\blook\s+(?:center|straight|ahead|forward)\b/i, /\bcenter\s+camera\b/i],
    sequence: [
      { endpoint: "/servo", method: "POST", body: { channel: 3, angle: SERVO_YAW_NEUTRAL } },
      { endpoint: "/servo", method: "POST", body: { channel: 4, angle: SERVO_PITCH_NEUTRAL } },
    ],
    description: "center camera",
  },
  // Beep / honk
  {
    patterns: [/\bbeep\b/i, /\bhonk\b/i],
    endpoint: "/beep",
    method: "POST",
    body: { duration: 200 },
    description: "beep",
  },
  // OTA update — REMOVED (security: no SSH to Vector from OpenClaw)
  // Camera feed — live video URL
  {
    patterns: [
      /\bcamera\b/i,
      /\bvideo\s+feed\b/i,
      /\blive\s+(?:stream|feed|video)\b/i,
      /\bshow\s+(?:me\s+)?(?:what\s+you\s+see|your\s+view|camera)\b/i,
    ],
    endpoint: "/camera",
    method: "GET",
    body: {},
    description: "get camera feed URL",
    isCamera: true,
  },
  // Full status dashboard
  {
    patterns: [/\bstatus\b/i, /\bhow\s+are\s+you\b/i, /\bdiagnostic/i],
    endpoint: "/status",
    method: "GET",
    body: {},
    description: "check status",
    isStatus: true,
  },
  // Health / sensors (after status to avoid conflict)
  {
    patterns: [/\bhealth\b/i, /\bsensors?\b/i],
    endpoint: "/health",
    method: "GET",
    body: {},
    description: "check health",
  },
];

/**
 * Parse a text message for robot movement intent.
 * Returns a command object if matched, or null if no command found.
 */
function parseCommand(text) {
  if (typeof text !== "string" || !text.trim()) return null;

  for (const cmd of COMMAND_MAP) {
    for (const pattern of cmd.patterns) {
      if (pattern.test(text)) {
        // OTA update commands removed (security: no SSH to Vector from OpenClaw)
        if (cmd.isPhoto) {
          return { isPhoto: true, description: cmd.description };
        }
        if (cmd.sequence) {
          return {
            sequence: cmd.sequence,
            description: cmd.description,
          };
        }
        const body = cmd.dynamicBody ? cmd.dynamicBody(text) : cmd.body;
        const result = {
          endpoint: cmd.endpoint,
          method: cmd.method,
          body,
          description: cmd.description,
        };
        if (cmd.port) result.port = cmd.port;
        if (cmd.isBattery) result.isBattery = true;
        if (cmd.isScene) result.isScene = true;
        if (cmd.isCamera) result.isCamera = true;
        if (cmd.isPatrol) result.isPatrol = true;
        if (cmd.isStatus) result.isStatus = true;
        if (cmd.isCall) result.isCall = true;
        return result;
      }
    }
  }
  return null;
}

/**
 * Execute a parsed command by sending an HTTP request to the Vector bridge.
 * Returns { success, status, data } or { success: false, error }.
 */
function executeCommand(command) {
  if (command.sequence) {
    return executeSequence(command.sequence);
  }
  if (ROBOT_COMMAND_TRANSPORT === "mqtt") {
    return executeMqttCommand(command);
  }
  if (ROBOT_COMMAND_TRANSPORT === "auto") {
    return executeMqttCommand(command).then((mqttResult) => {
      if (mqttResult.success) return mqttResult;
      return executeHttpCommand(command).then((httpResult) => {
        if (httpResult.success) return httpResult;
        const mqttReason = mqttResult.error || "MQTT publish failed";
        const httpReason = httpResult.error || `HTTP ${httpResult.status}`;
        return { success: false, error: `MQTT failed: ${mqttReason}; HTTP failed: ${httpReason}` };
      });
    });
  }
  return executeHttpCommand(command);
}

function executeSequence(steps) {
  let chain = Promise.resolve(null);
  for (const step of steps) {
    chain = chain.then((prev) => {
      if (prev && !prev.success) return prev;
      return executeCommand(step);
    });
  }
  return chain.then((result) => result || { success: false, error: "Empty sequence" });
}

function executeHttpCommand(command) {
  return new Promise((resolve) => {
    const hasBody = command.method !== "GET" && command.body !== undefined;
    const payload = hasBody ? JSON.stringify(command.body) : "";
    const headers = {
      "Content-Type": "application/json",
    };
    if (hasBody) {
      headers["Content-Length"] = Buffer.byteLength(payload);
    }
    const defaultTimeout = 10000;
    const options = {
      hostname: BRIDGE_HOST,
      port: command.port || BRIDGE_PORT,
      path: command.endpoint,
      method: command.method,
      headers,
      timeout: defaultTimeout,
    };

    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        let parsed;
        try {
          parsed = JSON.parse(data);
        } catch {
          parsed = data;
        }
        const httpSuccess = res.statusCode >= 200 && res.statusCode < 300;
        const semanticFailure = isSemanticFailure(parsed);
        if (httpSuccess && semanticFailure) {
          const reason = getSemanticFailureReason(parsed);
          resolve({ success: false, status: res.statusCode, data: parsed, error: reason });
          return;
        }
        resolve({ success: httpSuccess, status: res.statusCode, data: parsed });
      });
    });

    req.on("timeout", () => {
      req.destroy();
      resolve({ success: false, error: "Request timed out (10s)" });
    });

    req.on("error", (err) => {
      resolve({ success: false, error: err.message });
    });

    if (hasBody) req.write(payload);
    req.end();
  });
}

function executeMqttCommand(command) {
  return new Promise((resolve) => {
    const topic = buildMqttTopic(command.endpoint);
    const payload = JSON.stringify({
      endpoint: command.endpoint,
      method: command.method,
      body: command.body ?? {},
      description: command.description,
      sent_at: new Date().toISOString(),
    });
    const args = [
      "-h", MQTT_BROKER_HOST,
      "-p", String(MQTT_BROKER_PORT),
      "-t", topic,
      "-q", String(MQTT_QOS),
      "-m", payload,
    ];

    execFile(MQTT_PUBLISH_CMD, args, { timeout: MQTT_TIMEOUT_MS }, (error, stdout, stderr) => {
      if (error) {
        resolve({
          success: false,
          error: formatMqttError(error, stderr),
        });
        return;
      }
      const output = typeof stdout === "string" ? stdout.trim() : "";
      resolve({ success: true, status: 202, data: { status: "published", topic, output } });
    });
  });
}

function buildMqttTopic(endpoint) {
  const path = String(endpoint || "").replace(/^\/+/, "");
  return path ? `${MQTT_TOPIC_PREFIX}/${path}` : MQTT_TOPIC_PREFIX;
}

function formatMqttError(error, stderr) {
  const stderrText = typeof stderr === "string" ? stderr.trim() : "";
  if (error && (error.signal === "SIGTERM" || error.killed) && error.code === null) {
    return `MQTT publish timed out (${MQTT_TIMEOUT_MS}ms)`;
  }
  if (error && error.code === "ENOENT") {
    return `MQTT publish command not found: ${MQTT_PUBLISH_CMD}`;
  }
  if (stderrText) return stderrText;
  if (error && typeof error.message === "string" && error.message.trim()) return error.message.trim();
  return "MQTT publish failed";
}

function normalizeTransportMode(mode) {
  const value = String(mode || "").toLowerCase();
  if (value === "mqtt" || value === "auto" || value === "http") return value;
  return "http";
}

function normalizeMqttQos(qos) {
  const parsed = parseInt(qos, 10);
  if (Number.isInteger(parsed) && parsed >= 0 && parsed <= 2) return parsed;
  return 1;
}

function sanitizeTopicPrefix(prefix) {
  const raw = String(prefix || "").trim();
  if (!raw) return "robot/commands";
  return raw.replace(/^\/+/, "").replace(/\/+$/, "");
}

function normalizePort(port, fallback) {
  const parsed = parseInt(port, 10);
  if (Number.isInteger(parsed) && parsed > 0 && parsed <= 65535) return parsed;
  return fallback;
}

function normalizeTimeoutMs(timeoutMs, fallback) {
  const parsed = parseInt(timeoutMs, 10);
  if (Number.isInteger(parsed) && parsed > 0 && parsed <= 60000) return parsed;
  return fallback;
}

function isSemanticFailure(parsed) {
  if (!parsed || typeof parsed !== "object") return false;
  if (Array.isArray(parsed)) return false;
  const status = typeof parsed.status === "string" ? parsed.status.toLowerCase() : "";
  return status === "error" || status === "failed" || status === "failure" || status === "unavailable";
}

function getSemanticFailureReason(parsed) {
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return "Bridge returned semantic failure status";
  }
  if (typeof parsed.error === "string" && parsed.error.trim()) return parsed.error.trim();
  if (typeof parsed.message === "string" && parsed.message.trim()) return parsed.message.trim();
  return `Bridge status: ${parsed.status}`;
}

/**
 * Capture a photo from the Vector bridge camera and send it as a Signal attachment.
 * Returns { success, message } — the message is the Signal response text.
 */
function captureAndSendPhoto() {
  const ts = Date.now();
  const photoPath = `${PHOTO_SAVE_DIR}/robot-photo-${ts}.jpg`;
  const containerPhotoPath = `/tmp/robot-photo-${ts}.jpg`;

  return fetchBridgePhoto()
    .then((jpegBuffer) => {
      const fs = require("fs");
      fs.writeFileSync(photoPath, jpegBuffer);
      return sendSignalAttachment(photoPath, containerPhotoPath, "Here's what I see:");
    })
    .then((sendResult) => {
      // Clean up local file after sending
      try { require("fs").unlinkSync(photoPath); } catch { /* ignore */ }
      if (sendResult.success) {
        return { success: true, message: "Photo sent!" };
      }
      return { success: false, message: `Robot error: could not send photo — ${sendResult.error}` };
    })
    .catch((err) => {
      try { require("fs").unlinkSync(photoPath); } catch { /* ignore */ }
      return { success: false, message: `Robot error: could not capture photo — ${err.message}` };
    });
}

/**
 * Fetch JPEG image from bridge GET /capture endpoint.
 * Returns a Buffer containing the raw JPEG data.
 */
function fetchBridgePhoto() {
  return new Promise((resolve, reject) => {
    const options = {
      hostname: BRIDGE_HOST,
      port: BRIDGE_PORT,
      path: "/capture",
      method: "GET",
      timeout: 10000,
    };

    const req = http.request(options, (res) => {
      if (res.statusCode < 200 || res.statusCode >= 300) {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          const body = Buffer.concat(chunks).toString();
          reject(new Error(`Bridge returned HTTP ${res.statusCode}: ${body}`));
        });
        return;
      }
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => resolve(Buffer.concat(chunks)));
    });

    req.on("timeout", () => {
      req.destroy();
      reject(new Error("Request timed out (10s)"));
    });

    req.on("error", (err) => reject(err));
    req.end();
  });
}

/**
 * Send an image file as a Signal attachment via signal-cli RPC.
 * Copies file into the gateway container, then calls signal-cli send with attachments.
 */
function sendSignalAttachment(localPath, containerPath, caption) {
  return new Promise((resolve) => {
    // Step 1: docker cp file into gateway container
    execFile("sg", ["docker", "-c",
      `docker cp '${localPath}' '${BOT_CONTAINER}:${containerPath}'`],
    { timeout: 10000 }, (cpErr) => {
      if (cpErr) {
        resolve({ success: false, error: `docker cp failed: ${cpErr.message}` });
        return;
      }

      // Step 2: send via signal-cli RPC with attachment
      const payload = JSON.stringify({
        jsonrpc: "2.0",
        method: "send",
        params: {
          groupId: SIGNAL_GROUP_ID,
          message: caption,
          attachments: [containerPath],
        },
        id: 1,
      });

      // Write payload to temp file, copy into container, then curl
      const fs = require("fs");
      const payloadPath = `${PHOTO_SAVE_DIR}/sig-photo-payload.json`;
      fs.writeFileSync(payloadPath, payload);

      execFile("sg", ["docker", "-c",
        `docker cp '${payloadPath}' '${BOT_CONTAINER}:/tmp/sig-photo-payload.json'`],
      { timeout: 10000 }, (cp2Err) => {
        try { fs.unlinkSync(payloadPath); } catch { /* ignore */ }
        if (cp2Err) {
          resolve({ success: false, error: `payload docker cp failed: ${cp2Err.message}` });
          return;
        }

        execFile("sg", ["docker", "-c",
          `docker exec '${BOT_CONTAINER}' curl -sf -X POST ` +
          "http://127.0.0.1:8080/api/v1/rpc " +
          "-H 'Content-Type: application/json' " +
          "-d @/tmp/sig-photo-payload.json"],
        { timeout: 15000 }, (sendErr, stdout) => {
          // Clean up container files
          execFile("sg", ["docker", "-c",
            `docker exec '${BOT_CONTAINER}' rm -f '${containerPath}' /tmp/sig-photo-payload.json`],
          { timeout: 5000 }, () => { /* best effort cleanup */ });

          if (sendErr) {
            resolve({ success: false, error: `signal-cli send failed: ${sendErr.message}` });
            return;
          }
          resolve({ success: true, data: stdout });
        });
      });
    });
  });
}

/**
 * Format battery health response into a readable string with voltage and percentage.
 * 3S LiPo: 9.6V (empty) to 12.6V (full).
 */
function formatBatteryResponse(result) {
  if (!result.success) {
    return `Robot error: could not check battery — ${result.error || "unknown"}`;
  }
  const data = result.data || {};
  const voltage = data.battery_v || data.voltage || 0;
  const pct = Math.max(0, Math.min(100, Math.round(((voltage - 9.6) / 3.0) * 100)));
  return `🔋 Battery: ${voltage}V (${pct}%)`;
}

/**
 * Format scene description response.
 */
function formatSceneResponse(result) {
  if (!result.success) {
    return `Robot error: could not describe scene — ${result.error || "unknown"}`;
  }
  const data = result.data || {};
  return data.description || "No scene description available.";
}

/**
 * Format a TTS/say response into a readable string.
 */
function formatSayResponse(result) {
  if (!result.success) {
    return `Robot error: could not speak — ${result.error || "unknown"}`;
  }
  return "Robot is speaking your message.";
}

/**
 * Format waypoint navigation response.
 */
function formatNavigationResponse(result) {
  if (!result.success) {
    const data = result.data || {};
    const msg = data.message || result.error || "unknown error";
    return `Robot: navigation failed — ${msg}`;
  }
  const data = result.data || {};
  if (data.ok) {
    return `🗺️ ${data.message || "Navigating..."}`;
  }
  return `Robot: ${data.message || "Navigation request sent."}`;
}

/**
 * Format camera feed response with clickable LiveKit URL.
 */
function formatCameraResponse(result) {
  if (!result.success) {
    return `Robot error: camera feed not available — ${result.error || "unknown"}`;
  }
  const data = result.data || {};
  if (data.url) {
    return `📹 Robot camera feed:\n${data.url}`;
  }
  return `Robot error: camera feed not available — ${data.error || "no URL returned"}`;
}

/**
 * Format patrol response into a readable string.
 */
function formatPatrolResponse(result) {
  if (!result.success) {
    const data = result.data || {};
    const msg = data.message || result.error || "unknown error";
    return `Robot: patrol failed — ${msg}`;
  }
  const data = result.data || {};
  if (data.state) {
    // Status response
    const state = data.state;
    const wp = data.current_waypoint || "none";
    const loops = data.loop_count || 0;
    const detections = data.detections_reported || 0;
    return `🔄 Patrol: ${state} | waypoint: ${wp} | loops: ${loops} | detections: ${detections}`;
  }
  return `🔄 ${data.message || "Patrol command sent."}`;
}

/**
 * Format status dashboard response into a readable Signal message with emoji indicators.
 */
function formatStatusResponse(result) {
  if (!result.success) {
    return `Robot error: could not get status — ${result.error || "unknown"}`;
  }
  const d = result.data || {};
  const lines = ["\u{1F916} Robot Status Dashboard", ""];

  // Battery
  const bat = d.battery || {};
  if (bat.error) {
    lines.push("\u{1F50B} Battery: \u274C unavailable");
  } else {
    const icon = bat.level === "ok" ? "\u2705" : bat.level === "warning" ? "\u26A0\uFE0F" : "\u{1F534}";
    lines.push(`\u{1F50B} Battery: ${bat.voltage}V (${bat.percentage}%) ${icon}`);
    if (bat.charging) lines.push("   \u26A1 Charging");
  }

  // ROS2 nodes
  const nodes = d.nodes || {};
  if (nodes.error) {
    lines.push("\u{1F9E9} Nodes: \u274C unavailable");
  } else {
    const missing = nodes.missing || [];
    const running = (nodes.running || []).length;
    const expected = (nodes.expected || []).length;
    if (missing.length === 0) {
      lines.push(`\u{1F9E9} Nodes: \u2705 ${running}/${expected} running`);
    } else {
      lines.push(`\u{1F9E9} Nodes: \u26A0\uFE0F ${running}/${expected} running`);
      lines.push(`   Missing: ${missing.join(", ")}`);
    }
  }

  // Audio pipeline
  const audio = d.audio || {};
  if (audio.error) {
    lines.push("\u{1F3A4} Audio: \u274C unavailable");
  } else {
    const wake = audio.wake_word_model || "none";
    const icon = wake === "hey jarvis" ? "\u2705" : "\u26A0\uFE0F";
    lines.push(`\u{1F3A4} Audio: ${icon} wake="${wake}"`);
  }

  // Detection
  const det = d.detection || {};
  if (det.error) {
    lines.push("\u{1F441}\uFE0F Detection: \u274C unavailable");
  } else {
    const active = det.active;
    const objs = (det.objects || []).map(o => `${o.class_name}(${o.count})`).join(", ");
    lines.push(`\u{1F441}\uFE0F Detection: ${active ? "\u2705 active" : "\u26A0\uFE0F stale"}${objs ? " \u2014 " + objs : ""}`);
  }

  // Planner
  const plan = d.planner || {};
  if (plan.error) {
    lines.push("\u{1F9E0} Planner: \u274C unavailable");
  } else {
    const mode = plan.mode || "unknown";
    const follow = plan.follow_enabled ? "following" : "idle";
    lines.push(`\u{1F9E0} Planner: ${mode === "autonomous" ? "\u2705" : "\u{1F527}"} ${mode} (${follow})`);
  }

  // Temps
  const temps = d.temps || {};
  if (temps.error) {
    lines.push("\u{1F321}\uFE0F Temps: \u274C unavailable");
  } else {
    const entries = Object.entries(temps);
    if (entries.length > 0) {
      const parts = entries.slice(0, 4).map(([k, v]) => `${k}:${v}\u00B0C`);
      const maxTemp = Math.max(...entries.map(([, v]) => v));
      const icon = maxTemp > 80 ? "\u{1F534}" : maxTemp > 60 ? "\u26A0\uFE0F" : "\u2705";
      lines.push(`\u{1F321}\uFE0F Temps: ${icon} ${parts.join(", ")}`);
    } else {
      lines.push("\u{1F321}\uFE0F Temps: no data");
    }
  }

  // Memory
  const mem = d.memory || {};
  if (mem.error) {
    lines.push("\u{1F4BE} Memory: \u274C unavailable");
  } else {
    const icon = mem.used_pct > 90 ? "\u{1F534}" : mem.used_pct > 75 ? "\u26A0\uFE0F" : "\u2705";
    lines.push(`\u{1F4BE} Memory: ${icon} ${mem.used_pct}% used (${mem.available_mb}MB free)`);
  }

  // Uptime
  if (d.uptime) {
    lines.push(`\u23F1\uFE0F Uptime: ${d.uptime}`);
  }

  return lines.join("\n");
}

/**
 * Format call response — includes LiveKit join URL for "call me".
 */
function formatCallResponse(result) {
  if (!result.success) {
    return `Robot error: could not start call — ${result.error || "unknown"}`;
  }
  const data = result.data || {};
  if (data.join_url) {
    return `📞 Call started! Join here:\n${data.join_url}`;
  }
  if (data.message) {
    return `📞 ${data.message}`;
  }
  return "📞 Call ended.";
}

/**
 * Format a bridge response into a human-readable Signal message.
 */
function formatResponse(command, result) {
  if (command.isCall) return formatCallResponse(result);
  if (command.isStatus) return formatStatusResponse(result);
  if (command.isBattery) return formatBatteryResponse(result);
  if (command.isScene) return formatSceneResponse(result);
  if (command.isSay) return formatSayResponse(result);
  if (command.isNavigation) return formatNavigationResponse(result);
  if (command.isCamera) return formatCameraResponse(result);
  if (command.isPatrol) return formatPatrolResponse(result);
  if (result.success) {
    const detail = typeof result.data === "object" && result.data.status
      ? ` (${result.data.status})`
      : "";
    return `Robot: ${command.description}${detail}`;
  }
  const reason = result.error || `HTTP ${result.status}`;
  return `Robot error: could not ${command.description} — ${reason}`;
}

// executeUpdate — REMOVED (security: no SSH/OTA to Vector from OpenClaw)

/**
 * Handle a Signal text message end-to-end: parse, execute, format response.
 * Returns a response string, or null if the message isn't a robot command.
 */
async function handleMessage(text) {
  const command = parseCommand(text);
  if (!command) return null;

  // OTA update path removed (security: no SSH to Vector from OpenClaw)

  if (command.isPhoto) {
    const result = await captureAndSendPhoto();
    return result.message;
  }

  const result = await executeCommand(command);
  return formatResponse(command, result);
}

// CLI interface
if (require.main === module) {
  const text = process.argv.slice(2).join(" ");
  if (!text) {
    console.error("Usage: node robot-commands.js <message>");
    process.exit(1);
  }
  handleMessage(text).then((response) => {
    if (response === null) {
      console.log("No robot command recognized in:", text);
    } else {
      console.log(response);
    }
  });
}

module.exports = { parseCommand, executeCommand, formatResponse, formatBatteryResponse, formatSceneResponse, formatSayResponse, formatNavigationResponse, formatCameraResponse, formatPatrolResponse, formatStatusResponse, formatCallResponse, handleMessage, captureAndSendPhoto, fetchBridgePhoto, sendSignalAttachment, COMMAND_MAP, COLOR_MAP, EFFECT_MAP, parseLedColor, parseLedEffect };
