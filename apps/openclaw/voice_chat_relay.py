#!/usr/bin/env python3
"""Voice chat relay: MQTT listener that forwards unhandled voice to Claude CLI.

Subscribes to robot/voice/chat on the Vector robot's mosquitto broker.
Calls Claude Haiku for fast conversational responses.
Publishes responses to robot/voice/chat/response.

Run on the NUC: python3 apps/openclaw/voice_chat_relay.py
"""

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

import paho.mqtt.client as mqtt

log = logging.getLogger("voice-chat-relay")

MQTT_HOST = os.getenv("VOICE_RELAY_MQTT_HOST", "192.168.1.71")
MQTT_PORT = int(os.getenv("VOICE_RELAY_MQTT_PORT", "1883"))
REQUEST_TOPIC = "robot/voice/chat"
RESPONSE_TOPIC = "robot/voice/chat/response"
CLAUDE_BINARY = os.getenv("VOICE_RELAY_CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.getenv("VOICE_RELAY_MODEL", "haiku")
RESPONSE_TIMEOUT = int(os.getenv("VOICE_RELAY_TIMEOUT", "12"))

SYSTEM_PROMPT = """\
You are Vector, a small wheeled robot. You are brief, friendly, and slightly witty.
Keep responses to 1-2 short sentences. You are currently following a person around.

{scene_context}

Respond naturally to what the user said. Do not use markdown or emojis.
If asked what you see, describe the scene naturally.
If asked about yourself, you're a Yahboom ROSMASTER R2 robot with mecanum wheels.
"""


def _call_claude(text: str, scene: str) -> str:
    """Call Claude CLI with the user's text and scene context."""
    scene_context = f"You currently see: {scene}" if scene else "You can't see anything right now."
    prompt = SYSTEM_PROMPT.format(scene_context=scene_context) + f"\nUser says: \"{text}\"\n"

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="voice-chat-", suffix=".txt", delete=False
    ) as f:
        f.write(prompt)
        prompt_path = f.name

    try:
        with open(prompt_path, "r") as pf:
            result = subprocess.run(
                [CLAUDE_BINARY, "--dangerously-skip-permissions", "--model", CLAUDE_MODEL, "-p"],
                stdin=pf,
                capture_output=True,
                text=True,
                timeout=RESPONSE_TIMEOUT,
                env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
            )
        response = result.stdout.strip()
        if not response:
            log.warning("Claude returned empty response, stderr: %s", result.stderr[:200])
            return "Hmm, I'm not sure what to say."
        # Truncate long responses for TTS
        if len(response) > 300:
            response = response[:297] + "..."
        return response
    except subprocess.TimeoutExpired:
        log.warning("Claude timed out after %ds", RESPONSE_TIMEOUT)
        return "Sorry, I need a moment to think."
    except FileNotFoundError:
        log.error("Claude binary not found: %s", CLAUDE_BINARY)
        return "I can't chat right now."
    except Exception as exc:
        log.error("Claude call failed: %s", exc)
        return "Something went wrong."
    finally:
        try:
            os.unlink(prompt_path)
        except OSError:
            pass


def _on_connect(client, _userdata, _flags, rc, _properties=None):
    if rc != 0:
        log.error("MQTT connect failed: rc=%d", rc)
        return
    log.info("Connected to MQTT %s:%d, subscribing to %s", MQTT_HOST, MQTT_PORT, REQUEST_TOPIC)
    client.subscribe(REQUEST_TOPIC, qos=1)


def _on_message(client, _userdata, msg):
    """Handle incoming voice chat request."""
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("Invalid payload on %s", msg.topic)
        return

    request_id = data.get("id", "")
    text = data.get("text", "").strip()
    scene = data.get("scene", "")

    if not text:
        log.warning("Empty text in voice chat request")
        return

    log.info("Voice chat request [%s]: '%s' (scene: %s)", request_id, text, scene[:80])

    # Process in a thread to not block MQTT loop
    threading.Thread(
        target=_handle_request,
        args=(client, request_id, text, scene),
        daemon=True,
        name=f"chat-{request_id[:8]}",
    ).start()


def _handle_request(client, request_id: str, text: str, scene: str):
    """Generate response and publish back via MQTT."""
    start = time.monotonic()
    response = _call_claude(text, scene)
    elapsed = time.monotonic() - start

    payload = json.dumps({
        "id": request_id,
        "response": response,
        "latency_ms": int(elapsed * 1000),
    })

    client.publish(RESPONSE_TOPIC, payload, qos=1)
    log.info("Voice chat response [%s] (%.1fs): '%s'", request_id, elapsed, response[:80])


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [voice-chat-relay] %(levelname)s: %(message)s",
    )
    log.info("Starting voice chat relay (MQTT %s:%d, model %s)", MQTT_HOST, MQTT_PORT, CLAUDE_MODEL)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    except Exception as exc:
        log.error("Failed to connect to MQTT: %s", exc)
        sys.exit(1)

    stop_event = threading.Event()

    def _shutdown(*_):
        log.info("Shutting down voice chat relay")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    client.loop_start()
    stop_event.wait()
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
