// intercom-relay.js — NUC-side HTTP relay for robot-to-Signal messages.
//
// Receives POST /intercom/receive with {"text": "..."} from Vector bridge,
// sends the message to Signal group via signal-cli JSON-RPC inside openclaw-gateway.
//
// Usage:
//   node intercom-relay.js
//   PORT=8083 node intercom-relay.js
//
// Environment:
//   PORT           — HTTP listen port (default: 8083)
//   SIGNAL_GROUP_ID — Signal group ID (default: project group)
//   BOT_CONTAINER  — OpenClaw gateway container name (default: openclaw-gateway)

const http = require("http");
const { execFile } = require("child_process");
const fs = require("fs");

const PORT = parseInt(process.env.PORT || "8083", 10);
const SIGNAL_GROUP_ID = process.env.SIGNAL_GROUP_ID || "BUrA+nRRpsfdYgftby/jpJ7Ugy5PBzYWg89oNNr4nF4=";
const BOT_CONTAINER = process.env.BOT_CONTAINER || "openclaw-gateway";

/**
 * Send a text message to Signal group via signal-cli JSON-RPC.
 */
function sendSignalText(message) {
  return new Promise((resolve) => {
    const payload = JSON.stringify({
      jsonrpc: "2.0",
      method: "send",
      params: {
        groupId: SIGNAL_GROUP_ID,
        message,
      },
      id: 1,
    });

    const ts = Date.now();
    const payloadPath = `/tmp/intercom-payload-${ts}.json`;
    const containerPayloadPath = `/tmp/intercom-payload-${ts}.json`;
    fs.writeFileSync(payloadPath, payload);

    execFile("sg", ["docker", "-c",
      `docker cp '${payloadPath}' '${BOT_CONTAINER}:${containerPayloadPath}'`],
    { timeout: 10000 }, (cpErr) => {
      try { fs.unlinkSync(payloadPath); } catch { /* ignore */ }
      if (cpErr) {
        resolve({ success: false, error: `docker cp failed: ${cpErr.message}` });
        return;
      }

      execFile("sg", ["docker", "-c",
        `docker exec '${BOT_CONTAINER}' curl -sf -X POST ` +
        "http://127.0.0.1:8080/api/v1/rpc " +
        "-H 'Content-Type: application/json' " +
        `-d @${containerPayloadPath}`],
      { timeout: 15000 }, (sendErr, stdout) => {
        // Clean up container file
        execFile("sg", ["docker", "-c",
          `docker exec '${BOT_CONTAINER}' rm -f '${containerPayloadPath}'`],
        { timeout: 5000 }, () => { /* best effort */ });

        if (sendErr) {
          resolve({ success: false, error: `signal-cli send failed: ${sendErr.message}` });
          return;
        }
        resolve({ success: true, data: stdout });
      });
    });
  });
}

const server = http.createServer(async (req, res) => {
  const respond = (code, obj) => {
    const body = JSON.stringify(obj);
    res.writeHead(code, {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(body),
    });
    res.end(body);
  };

  if (req.method === "GET" && req.url === "/health") {
    respond(200, { status: "ok" });
    return;
  }

  if (req.method === "POST" && req.url === "/intercom/receive") {
    let rawBody = "";
    req.on("data", (chunk) => (rawBody += chunk));
    req.on("end", async () => {
      let data;
      try {
        data = JSON.parse(rawBody);
      } catch {
        respond(400, { error: "invalid JSON" });
        return;
      }

      const text = (data.text || "").trim();
      if (!text) {
        respond(400, { error: "text required" });
        return;
      }

      // Prefix with robot emoji so Ophir knows the source
      const prefixed = `\u{1F916} Robot: ${text}`;
      const result = await sendSignalText(prefixed);

      if (result.success) {
        console.log(`[intercom] Relayed: "${text.substring(0, 80)}"`);
        respond(200, { status: "sent" });
      } else {
        console.error(`[intercom] Failed: ${result.error}`);
        respond(502, { error: result.error });
      }
    });
    return;
  }

  respond(404, { error: "not found" });
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`[intercom-relay] Listening on port ${PORT}`);
});
