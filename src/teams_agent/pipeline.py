"""Pipecat pipeline: WebsocketServerTransport + GeminiLiveLLMService.

Speech-to-speech pipeline with TLS WebSocket server. The browser doesn't
connect directly — a Python-side bridge (bridge.py) acts as the WS client.

Audio routing:
  Browser JS → expose_function → bridge.py → WSS → Pipecat → Gemini Live
  Gemini Live → Pipecat → WSS → bridge.py → page.evaluate → Browser JS
"""

import asyncio
import logging
import os
import ssl
import time

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.google.gemini_live import GeminiLiveLLMService
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMSettings
from pipecat.transports.websocket.server import (
    WebsocketServerParams,
    WebsocketServerTransport,
)

from src.teams_agent.config import Config
from src.teams_agent.serializer import RawPCMSerializer

logger = logging.getLogger("teams_agent.pipeline")

# Project root (two levels up from src/teams_agent/)
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

    # Patch server to use TLS
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


def build_llm(cfg: Config) -> GeminiLiveLLMService:
    """Create Gemini Live speech-to-speech service with unlimited session duration."""
    return GeminiLiveLLMService(
        api_key=cfg.GOOGLE_API_KEY,
        system_instruction=cfg.SYSTEM_INSTRUCTION,
        settings=GeminiLiveLLMSettings(
            model=cfg.GEMINI_MODEL,
            voice=cfg.GEMINI_VOICE,
            context_window_compression={
                "enabled": True,
                "trigger_tokens": 90000,
            },
        ),
        # Pipecat handles goAway signals and session resumption automatically:
        # - Google sends goAway 60s before connection expires
        # - Pipecat requests a resumption token
        # - Opens a new WebSocket in the background
        # - Swaps the audio stream seamlessly between turns
    )


async def create_and_run_pipeline(
    shutdown_event: asyncio.Event | None = None,
    ws_ready_event: asyncio.Event | None = None,
):
    """Build and run the Pipecat pipeline. Blocks until pipeline stops."""
    cfg = Config()

    transport = build_transport(cfg)
    llm = build_llm(cfg)
    logger.info("GeminiLiveLLMService: model=%s, voice=%s", cfg.GEMINI_MODEL, cfg.GEMINI_VOICE)

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

    # Simple pipeline: no local turn detection / context aggregators.
    # Gemini Live has its own server-side VAD — Pipecat's local Smart Turn
    # detector was causing constant false interruptions from background noise.
    pipeline = Pipeline([
        transport.input(),
        llm,
        transport.output(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),
        idle_timeout_secs=None,  # Disable idle timeout — audio may pause between turns
    )

    # ── Session reliability: proactive refresh + stall watchdog ────────
    #
    # Despite context_window_compression and goAway handling, Gemini Live
    # sessions silently stall mid-conversation. Two defenses:

    # 1. Proactive refresh every 3 minutes (session resumption makes this seamless)
    REFRESH_SECS = 3 * 60

    async def _periodic_refresh():
        while True:
            await asyncio.sleep(REFRESH_SECS)
            logger.info("Proactive Gemini session refresh...")
            try:
                await llm._reconnect()
                logger.info("Gemini session refreshed")
            except Exception as e:
                logger.error("Refresh failed: %s", e)

    asyncio.create_task(_periodic_refresh())

    # 2. Stall watchdog: if no Gemini activity for 45s, force reconnect
    STALL_TIMEOUT = 45
    _last_gemini_activity = time.monotonic()

    # Track Gemini activity by wrapping process_frame
    _original_process_frame = llm.process_frame

    async def _tracked_process_frame(frame, direction):
        nonlocal _last_gemini_activity
        _last_gemini_activity = time.monotonic()
        await _original_process_frame(frame, direction)

    llm.process_frame = _tracked_process_frame

    async def _stall_watchdog():
        nonlocal _last_gemini_activity
        while True:
            await asyncio.sleep(10)
            stall = time.monotonic() - _last_gemini_activity
            if stall > STALL_TIMEOUT:
                logger.warning("Gemini stall detected (%.0fs no activity) — reconnecting", stall)
                try:
                    await llm._reconnect()
                    _last_gemini_activity = time.monotonic()
                    logger.info("Gemini reconnected after stall")
                except Exception as e:
                    logger.error("Stall watchdog reconnect failed: %s", e)

    asyncio.create_task(_stall_watchdog())

    if shutdown_event:
        async def _watch_shutdown():
            await shutdown_event.wait()
            logger.info("Shutdown event received, stopping pipeline...")
            await task.cancel()
        asyncio.create_task(_watch_shutdown())

    runner = PipelineRunner()
    logger.info("Pipeline starting...")
    await runner.run(task)
    logger.info("Pipeline stopped.")
