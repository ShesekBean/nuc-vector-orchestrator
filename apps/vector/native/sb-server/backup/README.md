# Porcupine v4 Backup Files

## Access Key
`CiN7N61EcNCWO/TEquj/nKgrWq487EtOEYB1gI9kwBm0Pj6y9GWFsw==`

## Files
- `pv_activation_cache_9b130781` — Activation cache from `/data/.pv/9b130781-porcupine` (CRITICAL: do not lose, last activation on this key)
- `hey_vector.ppn` — Wake word keyword file for "Hey Vector" (from `/anki/data/assets/cozmo_resources/assets/picovoice/`)
- `hey_cosmo.ppn` — Wake word keyword file for "Hey Cosmo" (from same dir)

## Restore
```bash
# On Vector (after mount -o remount,rw /):
scp backup/pv_activation_cache_9b130781 root@192.168.1.73:/data/.pv/9b130781-porcupine
scp backup/hey_vector.ppn root@192.168.1.73:/anki/data/assets/cozmo_resources/assets/picovoice/hey_vector.ppn
```

## Important
- HOME must be `/data/` (writable) not `/root/` (read-only)
- Never delete `/data/.pv/` directory
- Never reboot without confirming cache exists
