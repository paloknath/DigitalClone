"""Pipecat pipeline: WebsocketServerTransport + Deepgram Voice Agent API.

Deepgram orchestrates STT (Flux) → LLM (GPT-4o-mini) → TTS (Cartesia)
server-side in a single WebSocket. This pipeline wraps that connection
as a Pipecat FrameProcessor so the existing audio bridge works unchanged.

Audio routing (same as Google S2S pipeline):
  Browser JS → expose_function → bridge.py → WSS → Pipecat → DG Voice Agent
  DG Voice Agent → Pipecat → WSS → bridge.py → page.evaluate → Browser JS
"""

import asyncio
import logging
import os
import ssl

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
    StartInterruptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.websocket.server import (
    WebsocketServerParams,
    WebsocketServerTransport,
)

from deepgram import AsyncDeepgramClient
from deepgram.agent import (
    AgentV1AgentAudioDone,
    AgentV1AgentStartedSpeaking,
    AgentV1AgentThinking,
    AgentV1ConversationText,
    AgentV1Error,
    AgentV1Settings,
    AgentV1SettingsAgent,
    AgentV1SettingsAgentListen,
    AgentV1SettingsAgentListenProvider_V2,
    AgentV1SettingsApplied,
    AgentV1SettingsAudio,
    AgentV1SettingsAudioInput,
    AgentV1SettingsAudioOutput,
    AgentV1UserStartedSpeaking,
    AgentV1Warning,
    AgentV1Welcome,
)
from deepgram.types.speak_settings_v1 import SpeakSettingsV1
from deepgram.types.speak_settings_v1provider import SpeakSettingsV1Provider_Cartesia
from deepgram.types.cartesia_speak_provider_voice import CartesiaSpeakProviderVoice
from deepgram.types.think_settings_v1 import ThinkSettingsV1
from deepgram.types.think_settings_v1provider import ThinkSettingsV1Provider_OpenAi

from src.teams_agent.config import Config
from src.teams_agent.serializer import RawPCMSerializer

logger = logging.getLogger("teams_agent.pipeline_deepgram")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _create_ssl_context() -> ssl.SSLContext:
    """Create SSL context with self-signed localhost certificate."""
    certs_dir = os.path.join(_PROJECT_ROOT, "certs")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        os.path.join(certs_dir, "localhost.pem"),
        os.path.join(certs_dir, "localhost-key.pem"),
    )
    return ctx


def build_transport(cfg: Config) -> WebsocketServerTransport:
    """Create TLS WebsocketServerTransport. Connected by bridge.py, not browser."""
    transport = WebsocketServerTransport(
        params=WebsocketServerParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
            audio_in_channels=1,
            audio_out_channels=1,
            serializer=RawPCMSerializer(sample_rate=16000, num_channels=1),
        ),
        host="localhost",
        port=cfg.WS_PORT,
    )

    ssl_ctx = _create_ssl_context()
    input_transport = transport.input()

    async def _tls_server_handler():
        from websockets import serve as websocket_serve

        logger.info("Starting WSS server on localhost:%d", cfg.WS_PORT)
        async with websocket_serve(
            input_transport._client_handler,
            input_transport._host,
            input_transport._port,
            ssl=ssl_ctx,
        ) as server:
            await input_transport._callbacks.on_websocket_ready()
            await input_transport._stop_server_event.wait()

    input_transport._server_task_handler = _tls_server_handler
    return transport


class DeepgramVoiceAgentProcessor(FrameProcessor):
    """Pipecat FrameProcessor wrapping Deepgram Voice Agent WebSocket.

    Audio in (InputAudioRawFrame) → send_media() → DG Voice Agent
    DG Voice Agent → audio bytes → OutputAudioRawFrame → downstream
    """

    def __init__(
        self,
        *,
        api_key: str,
        system_prompt: str,
        listen_model: str = "flux-general-en",
        think_model: str = "gpt-4o-mini",
        speak_model_id: str = "sonic-2",
        speak_voice_id: str = "",
        input_sample_rate: int = 16000,
        output_sample_rate: int = 24000,
        transcript_collector=None,
    ):
        super().__init__()
        self._api_key = api_key
        self._system_prompt = system_prompt
        self._listen_model = listen_model
        self._think_model = think_model
        self._speak_model_id = speak_model_id
        self._speak_voice_id = speak_voice_id
        self._input_sample_rate = input_sample_rate
        self._output_sample_rate = output_sample_rate
        self._transcript_collector = transcript_collector

        self._client = AsyncDeepgramClient(api_key=api_key)
        self._socket = None
        self._connection_ctx = None
        self._receive_task = None
        self._ready = False  # True only after SettingsApplied received
        self._ready_event = asyncio.Event()  # Signaled when SettingsApplied arrives
        self._ws_closed = False  # Suppress error spam after disconnect
        self._suppressing_output = False  # Drop bot audio after user interrupt
        self._bot_is_speaking = False  # True while bot audio is being output

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            # Don't connect yet — DG times out if no audio arrives within ~10s.
            # Connect lazily on first audio frame.

        elif isinstance(frame, UserStartedSpeakingFrame):
            # Only suppress if bot is actively speaking (barge-in)
            if self._bot_is_speaking:
                self._suppressing_output = True
                logger.info("Interrupt received — suppressing bot audio (barge-in)")
                await self.push_frame(StartInterruptionFrame())
            await self.push_frame(frame, direction)

        elif isinstance(frame, InputAudioRawFrame):
            # Lazy connect: establish DG session on first audio frame
            if not self._socket and not self._ws_closed:
                await self._connect()

            if self._socket and self._ready and not self._ws_closed:
                try:
                    await self._socket.send_media(frame.audio)
                except Exception as e:
                    if not self._ws_closed:
                        self._ws_closed = True
                        logger.error("DG WebSocket closed: %s", e)

        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self._disconnect()
            await self.push_frame(frame, direction)

        else:
            await self.push_frame(frame, direction)

    async def _connect(self):
        """Connect to Deepgram Voice Agent and send settings."""
        logger.info("Connecting to Deepgram Voice Agent...")

        self._connection_ctx = self._client.agent.v1.connect()
        self._socket = await self._connection_ctx.__aenter__()
        self._ws_closed = False

        settings = AgentV1Settings(
            audio=AgentV1SettingsAudio(
                input=AgentV1SettingsAudioInput(
                    encoding="linear16",
                    sample_rate=self._input_sample_rate,
                ),
                output=AgentV1SettingsAudioOutput(
                    encoding="linear16",
                    sample_rate=self._output_sample_rate,
                    container="none",
                ),
            ),
            agent=AgentV1SettingsAgent(
                listen=AgentV1SettingsAgentListen(
                    provider=AgentV1SettingsAgentListenProvider_V2(
                        model=self._listen_model,
                    ),
                ),
                think=ThinkSettingsV1(
                    provider=ThinkSettingsV1Provider_OpenAi(
                        model=self._think_model,
                    ),
                    prompt=self._system_prompt,
                ),
                speak=SpeakSettingsV1(
                    provider=SpeakSettingsV1Provider_Cartesia(
                        model_id=self._speak_model_id,
                        voice=CartesiaSpeakProviderVoice(
                            mode="id",
                            id=self._speak_voice_id,
                        ),
                        language="en",
                    ),
                ),
            ),
        )

        await self._socket.send_settings(settings)
        logger.info("Deepgram Voice Agent settings sent — waiting for SettingsApplied...")

        # Start receive loop (will set _ready when SettingsApplied arrives)
        self._ready_event.clear()
        self._receive_task = asyncio.create_task(self._receive_loop())

        # Wait up to 5s for settings to be applied before sending audio
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error("DG Voice Agent: SettingsApplied timeout — check settings")

    async def _disconnect(self):
        """Close the Deepgram Voice Agent connection."""
        self._ready = False
        self._ws_closed = True

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._connection_ctx:
            try:
                await self._connection_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._connection_ctx = None
            self._socket = None

        logger.info("Deepgram Voice Agent disconnected")

    async def _receive_loop(self):
        """Receive audio and events from Deepgram Voice Agent."""
        try:
            async for message in self._socket:
                if self._ws_closed:
                    break

                if isinstance(message, bytes):
                    if self._suppressing_output:
                        continue  # Drop bot audio during interrupt
                    self._bot_is_speaking = True
                    frame = OutputAudioRawFrame(
                        audio=message,
                        sample_rate=self._output_sample_rate,
                        num_channels=1,
                    )
                    await self.push_frame(frame)

                elif isinstance(message, AgentV1Welcome):
                    logger.info("DG Voice Agent: Welcome (session started)")

                elif isinstance(message, AgentV1SettingsApplied):
                    self._ready = True
                    self._ready_event.set()
                    logger.info("DG Voice Agent: Settings applied — ready for audio")

                elif isinstance(message, AgentV1ConversationText):
                    logger.info(
                        "DG Voice Agent [%s]: %s",
                        message.role,
                        message.content,
                    )
                    if self._transcript_collector:
                        await self._transcript_collector.add_entry(
                            message.role, message.content
                        )

                elif isinstance(message, AgentV1UserStartedSpeaking):
                    # Only suppress if bot is actively speaking (barge-in)
                    if self._bot_is_speaking and not self._suppressing_output:
                        self._suppressing_output = True
                        await self.push_frame(StartInterruptionFrame())
                        logger.info("DG Voice Agent: User barge-in — suppressing bot audio")
                    else:
                        logger.info("DG Voice Agent: User started speaking")

                elif isinstance(message, AgentV1AgentThinking):
                    # Agent is thinking about new response — clear suppress
                    if self._suppressing_output:
                        self._suppressing_output = False
                        logger.info("DG Voice Agent: Agent thinking — output resumed")
                    else:
                        logger.info("DG Voice Agent: Agent thinking...")

                elif isinstance(message, AgentV1AgentStartedSpeaking):
                    self._suppressing_output = False
                    logger.info("DG Voice Agent: Agent started speaking")

                elif isinstance(message, AgentV1AgentAudioDone):
                    self._bot_is_speaking = False
                    if self._suppressing_output:
                        self._suppressing_output = False
                        logger.info("DG Voice Agent: Agent audio done — output resumed")
                    else:
                        logger.info("DG Voice Agent: Agent audio done")

                elif isinstance(message, AgentV1Error):
                    logger.error(
                        "DG Voice Agent error: %s (code=%s)",
                        getattr(message, "description", message),
                        getattr(message, "code", "unknown"),
                    )

                elif isinstance(message, AgentV1Warning):
                    logger.warning("DG Voice Agent warning: %s", message)

                else:
                    logger.debug("DG Voice Agent message: %s", type(message).__name__)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._ws_closed:
                self._ws_closed = True
                self._ready = False
                logger.error("DG receive loop error: %s", e)


async def create_and_run_pipeline(
    shutdown_event: asyncio.Event | None = None,
    ws_ready_event: asyncio.Event | None = None,
    transcript_collector=None,
    dg_processor_ref: list | None = None,
    system_prompt_override: str | None = None,
):
    """Build and run the Deepgram Voice Agent pipeline. Blocks until pipeline stops."""
    cfg = Config()

    transport = build_transport(cfg)

    system_prompt = system_prompt_override or cfg.SYSTEM_INSTRUCTION

    dg_processor = DeepgramVoiceAgentProcessor(
        api_key=cfg.DEEPGRAM_API_KEY,
        system_prompt=system_prompt,
        listen_model=cfg.DEEPGRAM_LISTEN_MODEL,
        think_model=cfg.DEEPGRAM_THINK_MODEL,
        speak_model_id=cfg.DEEPGRAM_SPEAK_MODEL,
        speak_voice_id=cfg.CARTESIA_VOICE_ID,
        transcript_collector=transcript_collector,
    )

    # Expose processor ref to caller (for VisionObserver access)
    if dg_processor_ref is not None:
        dg_processor_ref.clear()
        dg_processor_ref.append(dg_processor)

    logger.info(
        "DeepgramVoiceAgent: listen=%s, think=%s, speak=%s",
        cfg.DEEPGRAM_LISTEN_MODEL,
        cfg.DEEPGRAM_THINK_MODEL,
        cfg.DEEPGRAM_SPEAK_MODEL,
    )

    if ws_ready_event:

        @transport.event_handler("on_websocket_ready")
        async def on_ready(transport):
            logger.info("WSS server ready on port %d", cfg.WS_PORT)
            ws_ready_event.set()

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, websocket):
        logger.info("Audio bridge connected")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, websocket):
        logger.info("Audio bridge disconnected")

    pipeline = Pipeline(
        [
            transport.input(),
            dg_processor,
            transport.output(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
        idle_timeout_secs=None,
    )

    if shutdown_event:

        async def _watch_shutdown():
            await shutdown_event.wait()
            logger.info("Shutdown event received, stopping pipeline...")
            await task.cancel()

        asyncio.create_task(_watch_shutdown())

    runner = PipelineRunner()
    logger.info("Deepgram Voice Agent pipeline starting...")
    await runner.run(task)
    logger.info("Deepgram Voice Agent pipeline stopped.")
