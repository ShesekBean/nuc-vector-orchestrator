# OpenClaw Gateway Authentication (V3 Protocol)

## Overview

OpenClaw 2026.3.x requires **device-signed authentication** for WebSocket connections that need write scopes (`operator.write`). Token-only auth gets read access but **cannot** call `chat.send` or other state-changing methods.

## Auth Flow

```
1. Client connects to ws://127.0.0.1:18889
2. Server sends: connect.challenge { nonce, ts }
3. Client builds V3 signature payload (see below)
4. Client signs payload with Ed25519 private key
5. Client sends: connect { auth, device, role, scopes }
6. Server verifies signature → grants scopes → hello-ok
```

## Device Identity

Stored at `~/.openclaw/identity/device.json`:
```json
{
  "deviceId": "bb3f...",
  "publicKeyPem": "-----BEGIN PUBLIC KEY-----\n...",
  "privateKeyPem": "-----BEGIN PRIVATE KEY-----\n..."
}
```

Created automatically by `openclaw configure`. **Do not share the private key.**

## V3 Signature Payload

Pipe-separated string:
```
v3|{deviceId}|{clientId}|{clientMode}|{role}|{scopes}|{signedAtMs}|{gatewayToken}|{nonce}|{platform}|{deviceFamily}
```

| Field | Value |
|-------|-------|
| version | `v3` |
| deviceId | From device.json |
| clientId | `cli` (must be this exact value) |
| clientMode | `backend` (must be this exact value) |
| role | `operator` |
| scopes | `operator.admin,operator.read,operator.write` (comma-separated) |
| signedAtMs | The `ts` from `connect.challenge` (NOT current time) |
| gatewayToken | From `gateway.auth.token` in openclaw.json |
| nonce | The `nonce` from `connect.challenge` |
| platform | `linux` |
| deviceFamily | empty string |

## Connect Frame

```json
{
  "type": "req",
  "id": "<uuid>",
  "method": "connect",
  "params": {
    "minProtocol": 3,
    "maxProtocol": 3,
    "client": {
      "id": "cli",
      "displayName": "Your Client Name",
      "version": "1.0.0",
      "platform": "linux",
      "mode": "backend"
    },
    "role": "operator",
    "scopes": ["operator.admin", "operator.read", "operator.write"],
    "auth": {
      "token": "<gateway-token>"
    },
    "device": {
      "id": "<deviceId>",
      "publicKey": "<base64url-no-padding>",
      "signature": "<base64url-no-padding>",
      "signedAt": <challenge-ts>,
      "nonce": "<challenge-nonce>"
    }
  }
}
```

**CRITICAL:** `device` goes at `params` level, NOT inside `auth`.

## Key Formats

- **Public key:** Raw Ed25519 bytes, base64url encoded, no padding
- **Signature:** Ed25519 signature of UTF-8 payload bytes, base64url encoded, no padding

## Python Example

```python
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key, Encoding, PublicFormat
import base64, json, time

identity = json.load(open("~/.openclaw/identity/device.json"))
privkey = load_pem_private_key(identity["privateKeyPem"].encode(), password=None)
pubkey = load_pem_public_key(identity["publicKeyPem"].encode())

# After receiving connect.challenge with nonce and ts:
payload = f"v3|{identity['deviceId']}|cli|backend|operator|operator.admin,operator.read,operator.write|{ts}|{gateway_token}|{nonce}|linux|"
signature = privkey.sign(payload.encode())

pub_b64 = base64.urlsafe_b64encode(pubkey.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode().rstrip("=")
sig_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")
```

## chat.send Params

New in 3.x — requires `idempotencyKey`:
```json
{
  "method": "chat.send",
  "params": {
    "message": "hello",
    "sessionKey": "hook:voice",
    "idempotencyKey": "<uuid>"
  }
}
```

## Device Management

```bash
# List paired devices
openclaw devices list

# Remove a device
openclaw devices remove <deviceId>

# Rotate token with scopes
openclaw devices rotate --dev <deviceId> --role operator --scope operator.admin --scope operator.read --scope operator.write
```

## Lessons Learned

1. Token-only auth (no device identity) gets **zero operator scopes** in 3.x — gateway strips them
2. `signedAt` must use the challenge `ts`, not current time
3. `device` field goes at `params` level, not inside `auth`
4. V3 payload uses `|` separator (V2 also uses `|`, earlier versions used `\n`)
5. `client.id` must be `"cli"` and `client.mode` must be `"backend"` — other values rejected
