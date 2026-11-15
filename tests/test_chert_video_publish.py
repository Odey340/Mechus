import asyncio

import pytest

rtc = pytest.importorskip("livekit.rtc")

from koov.chert_livekit import LiveKitVideoCard


def test_capture_pushes_frame_into_real_video_source():
    """_capture must run against a real rtc.VideoSource without raising."""

    async def run() -> None:
        source = rtc.VideoSource(LiveKitVideoCard.WIDTH, LiveKitVideoCard.HEIGHT)
        card = LiveKitVideoCard(asyncio.get_running_loop(), source)
        await card._capture(0, "move_in", "apt-4b", "Connecting to FaceTime")
        await card._capture(3, "move_in", "apt-4b", "Inspection active")

    asyncio.run(run())


def test_capture_sends_a_720x1280_rgba_frame():
    """The frame handed to the source has the card's size and pixel format."""
    frames = []

    class FakeSource:
        def capture_frame(self, frame):  # sync, like rtc.VideoSource
            frames.append(frame)

    async def run() -> None:
        card = LiveKitVideoCard(asyncio.get_running_loop(), FakeSource())
        await card._capture(2, "move_in", "apt-4b", "Inspection active")

    asyncio.run(run())
    assert len(frames) == 1
    assert (frames[0].width, frames[0].height) == (720, 1280)
    assert frames[0].type == rtc.VideoBufferType.RGBA
