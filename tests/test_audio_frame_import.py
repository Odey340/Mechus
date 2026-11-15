"""Regression: AudioFrame must be importable (Zoom/RTMS/FaceTime workers depend on it)."""

from __future__ import annotations

import importlib

import pytest


def test_audio_frame_importable_from_mechus_audio() -> None:
    mod = importlib.import_module("mechus.audio")
    AudioFrame = getattr(mod, "AudioFrame", None)
    assert AudioFrame is not None, "mechus.audio.AudioFrame missing — worker join will fail"
    frame = AudioFrame(pcm16=b"\x00\x00", timestamp_s=1.0)
    assert frame.pcm16 == b"\x00\x00"
    assert frame.timestamp_s == 1.0


def test_zoom_and_rtms_inputs_import_audio_frame() -> None:
    importlib.import_module("mechus.zoom_sdk_join")
    importlib.import_module("mechus.rtms_input")
