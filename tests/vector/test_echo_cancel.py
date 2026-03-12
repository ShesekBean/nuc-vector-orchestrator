"""Tests for EchoSuppressor — echo suppression coordinator."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from apps.vector.src.events.event_types import TTS_PLAYING, TtsPlayingEvent
from apps.vector.src.events.nuc_event_bus import NucEventBus
from apps.vector.src.voice.echo_cancel import DEFAULT_HOLDOFF_SEC, EchoSuppressor


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def bus() -> NucEventBus:
    return NucEventBus()


@pytest.fixture()
def audio_client() -> MagicMock:
    """Mock AudioClient with clear_buffer method."""
    client = MagicMock()
    client.clear_buffer = MagicMock()
    return client


@pytest.fixture()
def suppressor(bus: NucEventBus, audio_client: MagicMock) -> EchoSuppressor:
    s = EchoSuppressor(bus, audio_client)
    yield s
    s.stop()


@pytest.fixture()
def suppressor_no_client(bus: NucEventBus) -> EchoSuppressor:
    s = EchoSuppressor(bus)
    yield s
    s.stop()


# ------------------------------------------------------------------
# Construction
# ------------------------------------------------------------------


class TestConstruction:
    def test_default_holdoff(self, suppressor: EchoSuppressor) -> None:
        assert suppressor.holdoff_sec == DEFAULT_HOLDOFF_SEC

    def test_custom_holdoff(self, bus: NucEventBus) -> None:
        s = EchoSuppressor(bus, holdoff_sec=2.0)
        assert s.holdoff_sec == 2.0
        s.stop()

    def test_not_active_initially(self, suppressor: EchoSuppressor) -> None:
        assert not suppressor.is_active


# ------------------------------------------------------------------
# holdoff_sec property
# ------------------------------------------------------------------


class TestHoldoffProperty:
    def test_set_holdoff(self, suppressor: EchoSuppressor) -> None:
        suppressor.holdoff_sec = 1.5
        assert suppressor.holdoff_sec == 1.5

    def test_negative_holdoff_raises(self, suppressor: EchoSuppressor) -> None:
        with pytest.raises(ValueError, match="holdoff_sec must be >= 0"):
            suppressor.holdoff_sec = -1.0

    def test_zero_holdoff_allowed(self, suppressor: EchoSuppressor) -> None:
        suppressor.holdoff_sec = 0.0
        assert suppressor.holdoff_sec == 0.0


# ------------------------------------------------------------------
# suppress_for()
# ------------------------------------------------------------------


class TestSuppressFor:
    def test_activates_suppression(self, suppressor: EchoSuppressor) -> None:
        suppressor.suppress_for(5.0)
        assert suppressor.is_active

    def test_expires_after_duration(self, suppressor: EchoSuppressor) -> None:
        suppressor.suppress_for(0.0)
        # Duration 0 means already expired (or within monotonic resolution)
        time.sleep(0.01)
        assert not suppressor.is_active


# ------------------------------------------------------------------
# TTS event handling
# ------------------------------------------------------------------


class TestTtsEvents:
    def test_tts_start_activates_suppression(
        self, bus: NucEventBus, suppressor: EchoSuppressor
    ) -> None:
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text="hello"))
        assert suppressor.is_active

    def test_tts_stop_sets_holdoff(
        self, bus: NucEventBus, suppressor: EchoSuppressor
    ) -> None:
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text="hello"))
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=False, text="hello"))
        # Should still be active during holdoff
        assert suppressor.is_active

    def test_holdoff_expires(
        self, bus: NucEventBus, audio_client: MagicMock
    ) -> None:
        s = EchoSuppressor(bus, audio_client, holdoff_sec=0.05)
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text="hi"))
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=False, text="hi"))
        time.sleep(0.1)
        assert not s.is_active
        s.stop()

    def test_tts_stop_flushes_buffer(
        self,
        bus: NucEventBus,
        audio_client: MagicMock,
        suppressor: EchoSuppressor,
    ) -> None:
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text="test"))
        audio_client.clear_buffer.assert_not_called()
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=False, text="test"))
        audio_client.clear_buffer.assert_called_once()

    def test_no_flush_without_audio_client(
        self, bus: NucEventBus, suppressor_no_client: EchoSuppressor
    ) -> None:
        # Should not raise even without audio_client
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text="x"))
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=False, text="x"))
        assert suppressor_no_client.is_active  # holdoff active

    def test_none_event_ignored(
        self, bus: NucEventBus, suppressor: EchoSuppressor
    ) -> None:
        bus.emit(TTS_PLAYING, None)
        assert not suppressor.is_active

    def test_event_without_playing_attr_ignored(
        self, bus: NucEventBus, suppressor: EchoSuppressor
    ) -> None:
        bus.emit(TTS_PLAYING, MagicMock(spec=[]))
        assert not suppressor.is_active


# ------------------------------------------------------------------
# stop()
# ------------------------------------------------------------------


class TestStop:
    def test_stop_unsubscribes(
        self,
        bus: NucEventBus,
        audio_client: MagicMock,
    ) -> None:
        s = EchoSuppressor(bus, audio_client)
        s.stop()
        # Events after stop should not affect suppressor
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text="after"))
        assert not s.is_active
        audio_client.clear_buffer.assert_not_called()


# ------------------------------------------------------------------
# Integration: full TTS cycle
# ------------------------------------------------------------------


class TestFullCycle:
    def test_full_tts_cycle(
        self,
        bus: NucEventBus,
        audio_client: MagicMock,
    ) -> None:
        s = EchoSuppressor(bus, audio_client, holdoff_sec=0.05)

        # Initially not active
        assert not s.is_active

        # TTS starts
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text="hello world"))
        assert s.is_active
        audio_client.clear_buffer.assert_not_called()

        # TTS stops — buffer flushed, holdoff active
        bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=False, text="hello world"))
        assert s.is_active
        audio_client.clear_buffer.assert_called_once()

        # After holdoff expires — suppression inactive
        time.sleep(0.1)
        assert not s.is_active

        s.stop()

    def test_multiple_tts_cycles(
        self,
        bus: NucEventBus,
        audio_client: MagicMock,
    ) -> None:
        s = EchoSuppressor(bus, audio_client, holdoff_sec=0.01)

        for i in range(3):
            bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=True, text=f"msg {i}"))
            assert s.is_active
            bus.emit(TTS_PLAYING, TtsPlayingEvent(playing=False, text=f"msg {i}"))
            time.sleep(0.02)

        assert audio_client.clear_buffer.call_count == 3
        assert not s.is_active

        s.stop()
