"""Python-side audio bridge between Playwright (browser) and Pipecat (WebSocket).

Features:
  - Rate-limited audio send (60ms chunks, 500ms buffer cap)
  - Client-side interruption detection: sends INTERRUPT_MARKER when user
    speaks over the bot, triggering Pipecat's pipeline interruption
  - Turn-end silence gap for short phrase reliability
  - DND mode for suppressing bot output
"""

import asyncio
import base64
import logging
import ssl
import struct
import time

import websockets

from src.teams_agent.serializer import INTERRUPT_MARKER

logger = logging.getLogger("teams_agent.bridge")


class AudioBridge:
    """Bridges Playwright page ↔ Pipecat WebSocket server."""

    def __init__(self, page, ws_port: int = 8765, silence_timeout: float = 10.0):
        self._page = page
        self._ws_url = f"wss://localhost:{ws_port}"
        self._ws = None
        self._running = False
        self._silence_timeout = silence_timeout

        # State
        self._dnd_mode = False
        self._bot_is_outputting = False
        self._bot_output_last = 0.0  # Timestamp of last bot audio chunk
        self._interrupt_sent = False
        self._last_interrupt_time = 0.0  # Debounce: min 1s between interrupts
        self._audio_buffer = bytearray()
        self._chunks_in = 0

    async def start(self):
        """Start the bridge: connect to Pipecat WS FIRST, then expose_function."""
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        self._ws = await websockets.connect(self._ws_url, ssl=ssl_ctx)
        logger.info("Connected to Pipecat WebSocket at %s", self._ws_url)

        await self._page.expose_function("sendAudioToPython", self._on_audio_from_browser)
        logger.info("Exposed sendAudioToPython function to browser")

        self._running = True
        asyncio.create_task(self._send_loop())
        asyncio.create_task(self._receive_loop())

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    # ── Audio input (browser → Pipecat) ──────────────────────────────

    async def _on_audio_from_browser(self, base64_audio: str):
        """Accumulate audio from browser."""
        audio_bytes = base64.b64decode(base64_audio)
        self._audio_buffer.extend(audio_bytes)
        self._chunks_in += 1
        if self._chunks_in <= 3 or self._chunks_in % 500 == 0:
            logger.info("Audio from browser: chunk #%d, %d bytes", self._chunks_in, len(audio_bytes))

        MAX_BUFFER = 8000
        if len(self._audio_buffer) > MAX_BUFFER:
            del self._audio_buffer[: len(self._audio_buffer) - MAX_BUFFER]

    @staticmethod
    def _audio_energy(pcm_bytes: bytes) -> float:
        """RMS energy of 16-bit PCM audio."""
        n_samples = len(pcm_bytes) // 2
        if n_samples == 0:
            return 0.0
        samples = struct.unpack(f"<{n_samples}h", pcm_bytes)
        return (sum(s * s for s in samples) / n_samples) ** 0.5

    async def _send_loop(self):
        """Send audio to Pipecat with client-side interruption detection."""
        CHUNK_BYTES = 1920  # 60ms at 16kHz
        INTERVAL = 0.06
        SPEECH_ENERGY = 100
        INTERRUPT_ENERGY = 300  # Higher threshold for interrupt detection
        TURN_END_SILENCE = 0.3

        last_speech_time = time.monotonic()
        was_speaking = False
        turn_end_sent = False
        ticks = 0
        chunks_sent = 0

        logger.info("Send loop started (60ms @ 16kHz)")
        while self._running:
            ticks += 1
            if ticks % 167 == 0:
                logger.info(
                    "Send loop: buffer=%d, sent=%d, bot_out=%s, dnd=%s",
                    len(self._audio_buffer), chunks_sent,
                    self._bot_is_outputting, self._dnd_mode,
                )

            if len(self._audio_buffer) >= CHUNK_BYTES:
                chunk = bytes(self._audio_buffer[:CHUNK_BYTES])
                del self._audio_buffer[:CHUNK_BYTES]

                energy = self._audio_energy(chunk)
                now = time.monotonic()

                if energy > SPEECH_ENERGY:
                    last_speech_time = now
                    was_speaking = True
                    turn_end_sent = False

                # Auto-expire bot output flag (500ms after last output chunk)
                if self._bot_is_outputting and (now - self._bot_output_last > 0.5):
                    self._bot_is_outputting = False
                    self._interrupt_sent = False  # Reset for next bot turn

                # Client-side interruption: ONE signal per bot turn, then stop
                if (
                    energy > INTERRUPT_ENERGY
                    and self._bot_is_outputting
                    and not self._interrupt_sent
                ):
                    self._interrupt_sent = True
                    self._bot_is_outputting = False  # Stop treating as bot output
                    logger.info("User interruption (energy=%.0f) — stopping bot", energy)
                    # 1. Send interrupt signal to Pipecat pipeline
                    if self._ws and not self._ws.close_code:
                        try:
                            await self._ws.send(INTERRUPT_MARKER)
                        except Exception:
                            pass
                    # 2. Clear browser playback immediately
                    try:
                        await self._page.evaluate("playbackBuffer = new Float32Array(0); window.botIsSpeaking = false;")
                    except Exception:
                        pass

                # Turn-end silence gap for short phrase detection
                silence_after_speech = now - last_speech_time
                if was_speaking and not turn_end_sent and silence_after_speech > TURN_END_SILENCE:
                    turn_end_sent = True
                    was_speaking = False
                    silence_chunk = b"\x00" * CHUNK_BYTES
                    if self._ws and not self._ws.close_code:
                        try:
                            for _ in range(3):
                                await self._ws.send(silence_chunk)
                        except Exception:
                            pass

                # Always send audio to Pipecat
                if self._ws and not self._ws.close_code:
                    try:
                        await self._ws.send(chunk)
                        chunks_sent += 1
                    except Exception as e:
                        logger.error("WS send error: %s", e)
                        break

            await asyncio.sleep(INTERVAL)

    # ── Audio output (Pipecat → browser) ─────────────────────────────

    async def _receive_loop(self):
        """Receive AI audio from Pipecat and push to browser."""
        try:
            async for message in self._ws:
                if isinstance(message, bytes) and len(message) > 0:
                    if self._dnd_mode:
                        continue

                    # Track bot output state — auto-expires after 500ms of no new chunks
                    self._bot_is_outputting = True
                    self._bot_output_last = time.monotonic()
                    # DON'T reset _interrupt_sent here — it's managed by the send loop

                    b64 = base64.b64encode(message).decode("ascii")
                    try:
                        await self._page.evaluate(
                            f"window.__playAudioFromPython('{b64}')"
                        )
                    except Exception:
                        pass
                else:
                    # Non-audio message or empty — bot may have stopped
                    self._bot_is_outputting = False
        except websockets.ConnectionClosed:
            logger.info("Pipecat WebSocket connection closed")
        except Exception as e:
            if self._running:
                logger.error("Bridge receive error: %s", e)
        finally:
            self._bot_is_outputting = False
