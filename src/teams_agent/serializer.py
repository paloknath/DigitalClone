"""Custom serializer for raw PCM binary audio over WebSocket.

Supports two message types:
- Normal audio: raw 16-bit PCM bytes (len > 4)
- Interrupt signal: 4-byte marker \xFF\xFE\xFD\xFC → UserStartedSpeakingFrame
"""

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    UserStartedSpeakingFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

# 4-byte marker sent by the bridge when user interrupts the bot
INTERRUPT_MARKER = b"\xff\xfe\xfd\xfc"


class RawPCMSerializer(FrameSerializer):
    """Treats binary WebSocket messages as raw 16-bit LE PCM audio,
    with support for an out-of-band interrupt signal."""

    def __init__(self, sample_rate: int = 16000, num_channels: int = 1):
        super().__init__()
        self._sample_rate = sample_rate
        self._num_channels = num_channels

    async def serialize(self, frame: Frame) -> bytes | None:
        """OutputAudioRawFrame → raw PCM bytes sent to browser."""
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Deserialize: raw PCM → InputAudioRawFrame, or interrupt marker → UserStartedSpeakingFrame."""
        if not isinstance(data, bytes) or len(data) == 0:
            return None

        # Interrupt signal from bridge (user speaking over bot)
        if data == INTERRUPT_MARKER:
            return UserStartedSpeakingFrame()

        return InputAudioRawFrame(
            audio=data,
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
        )
