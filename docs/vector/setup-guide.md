# Vector Setup Guide

How to set up Vector 2.0 (OSKR) with wire-pod on the NUC from scratch. Written from experience on 2026-03-11.

## Prerequisites

- Vector 2.0 with OSKR unlock
- NUC running wire-pod (ports 8080 HTTP, 443 TLS/gRPC)
- Chrome browser with Bluetooth (on laptop or any BLE-capable machine)
- Python 3.x on NUC

## Step 1: Connect Vector to WiFi via BLE

1. Put Vector in pairing mode: hold his back button for ~15 seconds until he shows a pairing screen
2. Open **https://vector-web-setup.anki.bot** in Chrome (requires Bluetooth). If the site is down, use the local mirror: `bash infra/vector/web-setup/serve.sh` then open `http://localhost:8000`
3. Click "Pair with Vector" and select your Vector from the Bluetooth dialog
4. Enter the 6-digit PIN shown on Vector's screen
5. Select your WiFi network and enter the password
6. Wait for Vector to confirm connection

**Note:** wire-pod's built-in BLE setup (`http://localhost:8080/setup.html`) can also work, but the official Anki site was more reliable in our experience. wire-pod's `rts-patched.js` needs a `case 7:` mapping to `RtsV5Handler` for RTS v7 Vectors. A local mirror of the Anki site is at `infra/vector/web-setup/` in case the upstream goes down.

## Step 2: Recover the OSKR SSH Key (if not saved)

If you lost the SSH private key from the original OSKR unlock:

1. Open **https://vector-setup.ddl.io** in Chrome
2. Pair with Vector via Bluetooth
3. Click **"Download Logs"** (top-right)
4. Extract the tar.bz2 archive (Python: `tarfile.open(path, 'r:bz2')`)
5. SSH key is at: `data/ssh/id_rsa_Vector-XXXX` (XXXX = robot ID, e.g. D2C9)

Install the key:
```bash
cp data/ssh/id_rsa_Vector-D2C9 ~/.ssh/
chmod 600 ~/.ssh/id_rsa_Vector-D2C9
```

## Step 3: Configure SSH Access

Vector's old SSH server only supports legacy RSA. Add to `~/.ssh/config`:

```
Host vector
    HostName <VECTOR_IP>
    User root
    IdentityFile ~/.ssh/id_rsa_Vector-D2C9
    PubkeyAcceptedAlgorithms +ssh-rsa
    HostKeyAlgorithms +ssh-rsa
    StrictHostKeyChecking no
```

Without `PubkeyAcceptedAlgorithms +ssh-rsa`, you get: `sign_and_send_pubkey: no mutual signature supported`

Test: `ssh vector "uname -a"` → should show `Linux Vector-D2C9 3.18.66-perf ...`

## Step 4: Deploy wire-pod Config via SSH Setup API

wire-pod has a built-in SSH setup endpoint that deploys everything:

```bash
curl -F "ip=<VECTOR_IP>" -F "key=@~/.ssh/id_rsa_Vector-D2C9" http://localhost:8080/api-ssh/setup
```

Monitor progress:
```bash
curl http://localhost:8080/api-ssh/get_setup_status
```

This will:
1. Stop Vector's robot services
2. Reset to onboarding mode (faces/photos persist)
3. Generate new robot.pem certificate
4. Deploy `server_config.json` pointing to NUC IP (to `/data/data/server_config.json`)
5. Deploy wire-pod TLS cert (to `/data/data/wirepod-cert.crt`)
6. Replace vic-cloud binary with wire-pod compatible version
7. Restart services

**The "Generating new robot certificate" step takes 1-2 minutes** on Vector's slow Snapdragon 212.

After it completes, **reboot Vector**: `ssh vector "reboot"`

## Step 5: Install Python SDK

```bash
pip3 install wirepod-vector-sdk
```

This installs as `anki_vector` module. **Do NOT have `anki-vector` (0.6.0) installed simultaneously** — they share the `anki_vector` namespace and the old one has protobuf v4 incompatibility.

## Step 6: Authenticate SDK with Vector

```python
import grpc, socket, configparser, os
from pathlib import Path
from anki_vector import messaging

serial = "<ESN>"     # e.g. "0dd1cdcf" — found at /factory/cloud/esn on Vector
name = "<NAME>"      # e.g. "Vector-D2C9" — from Vector's TLS cert CN
ip = "<VECTOR_IP>"   # e.g. "192.168.1.73"

# Get Vector's self-signed TLS cert
# Run: echo | openssl s_client -connect <IP>:443 2>/dev/null | openssl x509 -outform PEM > cert.pem
cert_file = str(Path.home() / ".anki_vector" / f"{name}-{serial}.cert")
with open(cert_file, "rb") as f:
    cert = f.read()

creds = grpc.ssl_channel_credentials(root_certificates=cert)
channel = grpc.secure_channel(f"{ip}:443", creds,
    options=(("grpc.ssl_target_name_override", name),))
grpc.channel_ready_future(channel).result(timeout=10)

interface = messaging.client.ExternalInterfaceStub(channel)
token = "2vMhFgktH3Jrbemm2WHkfGN"  # wire-pod hardcoded session token
request = messaging.protocol.UserAuthenticationRequest(
    user_session_id=token.encode('utf-8'),
    client_name=socket.gethostname().encode('utf-8'))
response = interface.UserAuthentication(request)
guid = response.client_token_guid

# Save config
os.makedirs(str(Path.home() / ".anki_vector"), exist_ok=True)
config = configparser.ConfigParser()
config[serial] = {
    "cert": cert_file, "ip": ip, "name": name,
    "guid": guid.decode('utf-8')
}
with open(str(Path.home() / ".anki_vector" / "sdk_config.ini"), 'w') as f:
    config.write(f)
```

## Step 7: Test

```python
import anki_vector

robot = anki_vector.Robot(serial="0dd1cdcf", default_logging=False)
robot.connect()
robot.behavior.say_text("Hello! I am programmable!")
batt = robot.get_battery_state()
print(f"Battery: {batt.battery_volts:.2f}V")
robot.disconnect()
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `sign_and_send_pubkey: no mutual signature supported` | Modern SSH disables RSA | Add `PubkeyAcceptedAlgorithms +ssh-rsa` to SSH config |
| `Permission denied (publickey)` | Wrong/missing SSH key | Recover key via BLE log download (Step 2) |
| vic-cloud exits with status 246 | Service file stale after binary swap | `systemctl daemon-reload && reboot` on Vector |
| `no bots are authenticated` in wire-pod | Vector hasn't connected yet | Reboot Vector, wait 30-60s for services to start |
| SDK `UNAUTHENTICATED` 401 | Empty GUID / wrong token | Re-run authentication (Step 6) after wire-pod setup |
| `Descriptors cannot be created directly` | anki-vector 0.6.0 + modern protobuf | Uninstall anki-vector, install wirepod-vector-sdk |
| Vector shows "800 anki.bot/support" | In onboarding mode after setup | Expected — completes after vic-cloud connects to wire-pod |

## Key Paths

| What | Location |
|------|----------|
| SSH key | `~/.ssh/id_rsa_Vector-D2C9` |
| SDK config | `~/.anki_vector/sdk_config.ini` |
| SDK cert | `~/.anki_vector/Vector-D2C9-0dd1cdcf.cert` |
| wire-pod server config (on Vector) | `/data/data/server_config.json` |
| wire-pod cert (on Vector) | `/data/data/wirepod-cert.crt` |
| Original server config (on Vector) | `/anki/data/assets/cozmo_resources/config/server_config.json` |
| Vector ESN | `/factory/cloud/esn` (on Vector) |
| vic-cloud binary (on Vector) | `/anki/bin/vic-cloud` |
