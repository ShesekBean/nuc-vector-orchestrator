#!/usr/bin/env python3
"""Standalone mic streaming test — stream audio, measure rate, save WAV.

Run: python3 apps/vector/tests/standalone/test_mic.py
"""

import os
import sys
import time

import anki_vector

# Ensure repo root is on path for imports
_repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from apps.vector.src.voice.audio_client import (  # noqa: E402
    AudioClient,
    TARGET_SAMPLE_RATE,
)

SERIAL = "0dd1cdcf"
SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def run_test(name, fn):
    print(f"  [{name}] ", end="", flush=True)
    t0 = time.time()
    try:
        result = fn()
        elapsed = time.time() - t0
        suffix = f" — {result}" if result else ""
        print(f"PASS ({elapsed:.2f}s){suffix}")
        return True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"FAIL ({elapsed:.2f}s) — {e}")
        return False


def main():
    print("=" * 60)
    print("STANDALONE TEST: Mic Audio Streaming")
    print("=" * 60)

    robot = anki_vector.Robot(serial=SERIAL, default_logging=False)
    results = []
    os.makedirs(SAMPLE_DIR, exist_ok=True)

    try:
        robot.connect()
        print(f"Connected to Vector-{SERIAL[:4]}\n")

        client = AudioClient(robot, buffer_size=100)

        # Test 1: Start stream
        def test_start():
            client.start()
            assert client.is_streaming, "Stream did not start"

        results.append(run_test("Start audio stream", test_start))

        # Test 2: Receive chunks (collect for 3 seconds)
        def test_receive():
            time.sleep(3)
            count = client.chunk_count
            if count == 0:
                raise RuntimeError("No audio chunks received after 3s")
            cps = client.chunks_per_second
            return f"{count} chunks, {cps:.1f} chunks/s"

        results.append(run_test("Receive audio chunks", test_receive))

        # Test 3: Read PCM buffer
        def test_read_pcm():
            pcm = client.read_pcm(1.0)
            if len(pcm) == 0:
                raise RuntimeError("read_pcm returned empty")
            n_samples = len(pcm) // 2
            duration = n_samples / TARGET_SAMPLE_RATE
            return f"{n_samples} samples, {duration:.2f}s at {TARGET_SAMPLE_RATE} Hz"

        results.append(run_test("Read PCM buffer", test_read_pcm))

        # Test 4: Beamforming metadata
        def test_beamforming():
            direction = client.source_direction
            confidence = client.source_confidence
            return f"direction={direction}, confidence={confidence}"

        results.append(run_test("Beamforming metadata", test_beamforming))

        # Test 5: Write WAV file
        def test_wav():
            wav_path = os.path.join(SAMPLE_DIR, "mic_sample.wav")
            n_samples = client.write_wav(wav_path, 2.0)
            if n_samples == 0:
                raise RuntimeError("WAV write produced 0 samples")
            size = os.path.getsize(wav_path)
            return f"{wav_path} ({size} bytes, {n_samples} samples)"

        results.append(run_test("Write WAV sample", test_wav))

        # Test 6: Latency benchmark
        def test_latency():
            # Measure time between chunk arrivals
            buf = client.get_audio_buffer()
            if len(buf) < 2:
                raise RuntimeError("Not enough chunks for latency calc")
            # Chunk size in seconds
            chunk_duration = (len(buf[0]) // 2) / TARGET_SAMPLE_RATE
            cps = client.chunks_per_second
            if cps > 0:
                interval_ms = 1000 / cps
            else:
                interval_ms = 0
            return f"chunk={chunk_duration*1000:.1f}ms, interval={interval_ms:.1f}ms"

        results.append(run_test("Latency benchmark", test_latency))

        # Test 7: Stop stream
        def test_stop():
            client.stop()
            assert not client.is_streaming, "Stream did not stop"

        results.append(run_test("Stop audio stream", test_stop))

    finally:
        try:
            if client.is_streaming:
                client.stop()
        except Exception:
            pass
        robot.disconnect()

    print()
    passed = sum(results)
    total = len(results)
    status = "PASS" if all(results) else "FAIL"
    print(f"Result: {status} ({passed}/{total})")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
