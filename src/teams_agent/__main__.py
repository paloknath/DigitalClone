"""Main orchestrator: launches Pipecat pipeline, then browser for Teams Audio Agent.

Usage:
    python run.py                         # Uses TEAMS_MEETING_URL from .env
    python -m src.teams_agent             # Same
    python api_server.py                  # API server mode

Requires:
    - .env file with pipeline-specific API keys
    - Set PIPELINE_MODE=google_s2s (default) or PIPELINE_MODE=deepgram_voice_agent
    - No drivers or virtual audio devices needed (driverless WebSocket bridge)
"""

import asyncio
import ctypes
import logging
import sys

from src.teams_agent.browser import (
    join_teams_meeting,
    leave_meeting,
    monitor_meeting,
    setup_audio_capture,
)
from src.teams_agent.bridge import AudioBridge
from src.teams_agent.config import Config
from src.teams_agent.transcript import TranscriptCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("teams_agent")


def set_high_priority():
    """Set the current process to High priority on Windows."""
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(handle, 0x00000080)
        logger.info("Process priority set to HIGH")


class BotSession:
    """Manages a single bot session in a Teams meeting."""

    def __init__(self):
        self.shutdown_event = asyncio.Event()
        self._pipeline_task = None
        self._bridge = None
        self._page = None
        self._browser = None
        self._transcript_collector = TranscriptCollector()
        self._meeting_memory = None
        self._vision_observer = None
        self._dg_processor_ref = [None]
        self.status = "idle"  # idle → starting → joined → ready → running → stopped

    async def start(
        self,
        meeting_url: str,
        bot_name: str = "AI Assistant",
        ready_callback=None,
        vision_enabled: bool | None = None,
    ):
        """Start the bot session. Blocks until the meeting ends or shutdown is triggered.

        Args:
            meeting_url: Teams meeting URL to join.
            bot_name: Display name for the bot in the meeting.
            ready_callback: Optional async callable invoked when bot is fully ready.
            vision_enabled: Override VISION_ENABLED from .env. None = use .env value.
        """
        cfg = Config()
        set_high_priority()

        self.status = "starting"
        ws_ready_event = asyncio.Event()
        is_deepgram = cfg.PIPELINE_MODE == "deepgram_voice_agent"

        # 0. Pre-meeting: retrieve past context from ChromaDB
        augmented_prompt = cfg.SYSTEM_INSTRUCTION
        if is_deepgram:
            try:
                from src.teams_agent.meeting_memory import MeetingMemory

                self._meeting_memory = MeetingMemory(
                    db_path=cfg.CHROMADB_PATH,
                    google_api_key=cfg.GOOGLE_API_KEY,
                    gemini_model=cfg.GEMINI_SUMMARY_MODEL,
                )
                past_context = await self._meeting_memory.retrieve_context(
                    query=cfg.SYSTEM_INSTRUCTION,
                    meeting_url=meeting_url,
                )
                if past_context:
                    augmented_prompt += (
                        "\n\n=== PAST MEETING CONTEXT ===\n"
                        + past_context
                        + "\n=== END PAST CONTEXT ==="
                    )
                    logger.info("Injected past meeting context (%d chars)", len(past_context))
            except Exception as e:
                logger.warning("Failed to retrieve past meeting context: %s", e)

        # 1. Select and start pipeline
        if is_deepgram:
            from src.teams_agent.pipeline_deepgram import create_and_run_pipeline

            logger.info("Starting Deepgram Voice Agent pipeline...")
            self._pipeline_task = asyncio.create_task(
                create_and_run_pipeline(
                    self.shutdown_event,
                    ws_ready_event,
                    transcript_collector=self._transcript_collector,
                    dg_processor_ref=self._dg_processor_ref,
                    system_prompt_override=augmented_prompt,
                )
            )
        else:
            from src.teams_agent.pipeline import create_and_run_pipeline

            logger.info("Starting Google S2S pipeline...")
            self._pipeline_task = asyncio.create_task(
                create_and_run_pipeline(self.shutdown_event, ws_ready_event)
            )

        try:
            await asyncio.wait_for(ws_ready_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            logger.error("WSS server failed to start")
            self.status = "stopped"
            self.shutdown_event.set()
            await self._pipeline_task
            return

        # 2. Launch browser and join Teams meeting
        logger.info("Joining Teams meeting: %s", meeting_url)
        self._browser, self._page, _ = await join_teams_meeting(meeting_url, bot_name)
        self.status = "joined"

        # 3. Start audio bridge
        self._bridge = AudioBridge(
            self._page, ws_port=cfg.WS_PORT, silence_timeout=cfg.SILENCE_TIMEOUT
        )
        await self._bridge.start()
        logger.info("Audio bridge started — sendAudioToPython ready")

        # 4. Set up audio capture + track injection
        await setup_audio_capture(self._page)
        logger.info("Audio capture and track injection complete")

        # 5. Dismiss camera permission notification (appears after audio setup)
        try:
            dismiss_btn = self._page.locator(
                '[data-tid="callingAlertDismissButton_VideoCapturePermissionDenied"]'
            )
            await asyncio.sleep(2)
            if await dismiss_btn.count() > 0 and await dismiss_btn.first.is_visible():
                await dismiss_btn.first.click()
                logger.info("Dismissed camera permission notification")
        except Exception:
            pass

        # 6. Start vision observer (Deepgram pipeline only)
        use_vision = vision_enabled if vision_enabled is not None else cfg.VISION_ENABLED
        self._augmented_prompt = augmented_prompt
        if is_deepgram and use_vision:
            try:
                # Wait for dg_processor_ref to be populated
                for _ in range(50):
                    if self._dg_processor_ref[0] is not None:
                        break
                    await asyncio.sleep(0.1)

                if self._dg_processor_ref[0]:
                    from src.teams_agent.vision_observer import VisionObserver

                    self._vision_observer = VisionObserver(
                        page=self._page,
                        dg_processor=self._dg_processor_ref[0],
                        google_api_key=cfg.GOOGLE_API_KEY,
                        gemini_model=cfg.GEMINI_VISION_MODEL,
                        think_model=cfg.DEEPGRAM_THINK_MODEL,
                        interval=cfg.VISION_INTERVAL,
                        base_prompt=cfg.SYSTEM_INSTRUCTION,
                    )
                    await self._vision_observer.start()
                else:
                    logger.warning("DG processor not ready, skipping vision observer")
            except Exception as e:
                logger.warning("Failed to start vision observer: %s", e)

        self.status = "ready"
        logger.info("Bot is READY — meeting joined, pipeline active, audio flowing")

        if ready_callback:
            await ready_callback()

        # 6. Run pipeline and meeting monitor concurrently
        self.status = "running"
        try:
            await asyncio.gather(
                self._pipeline_task,
                monitor_meeting(self._page, self.shutdown_event),
            )
        except KeyboardInterrupt:
            logger.info("Interrupted by user.")
            self.shutdown_event.set()
        except Exception:
            logger.exception("Unexpected error:")
            self.shutdown_event.set()
        finally:
            logger.info("Shutting down...")
            self.shutdown_event.set()

            # Stop vision observer first
            if self._vision_observer:
                await self._vision_observer.stop()

            # Generate meeting summary and store in ChromaDB
            if self._meeting_memory and self._transcript_collector.entry_count > 0:
                try:
                    transcript_text = self._transcript_collector.get_formatted_transcript()
                    logger.info(
                        "Generating meeting summary (%d entries)...",
                        self._transcript_collector.entry_count,
                    )
                    summary = await self._meeting_memory.generate_summary(
                        transcript_text=transcript_text,
                        meeting_url=meeting_url,
                    )
                    logger.info("Summary generated (%d chars)", len(summary))
                    mid = await self._meeting_memory.store_meeting(
                        meeting_url=meeting_url,
                        summary=summary,
                        transcript_text=transcript_text,
                    )
                    logger.info("Meeting stored in ChromaDB: %s", mid)
                except Exception as e:
                    logger.error("Failed to generate/store meeting summary: %s", e)

            if self._bridge:
                await self._bridge.stop()
            if self._page and self._browser:
                await leave_meeting(self._page, self._browser)
            self.status = "stopped"
            logger.info("Bot shut down cleanly.")

    async def start_vision(self) -> bool:
        """Start the vision observer on a running session. Returns True if started."""
        if self._vision_observer:
            return True  # Already running

        if not self._dg_processor_ref[0]:
            logger.warning("Cannot start vision: DG processor not available")
            return False

        if not self._page:
            logger.warning("Cannot start vision: no active page")
            return False

        try:
            cfg = Config()
            from src.teams_agent.vision_observer import VisionObserver

            self._vision_observer = VisionObserver(
                page=self._page,
                dg_processor=self._dg_processor_ref[0],
                google_api_key=cfg.GOOGLE_API_KEY,
                gemini_model=cfg.GEMINI_VISION_MODEL,
                think_model=cfg.DEEPGRAM_THINK_MODEL,
                interval=cfg.VISION_INTERVAL,
                base_prompt=cfg.SYSTEM_INSTRUCTION,
            )
            await self._vision_observer.start()
            logger.info("Vision observer started via API")
            return True
        except Exception as e:
            logger.error("Failed to start vision observer: %s", e)
            return False

    async def stop_vision(self) -> bool:
        """Stop the vision observer. Returns True if stopped."""
        if not self._vision_observer:
            return True  # Already stopped
        await self._vision_observer.stop()
        self._vision_observer = None
        logger.info("Vision observer stopped via API")
        return True

    @property
    def vision_active(self) -> bool:
        return self._vision_observer is not None and self._vision_observer._running

    async def stop(self):
        """Signal the bot to leave the meeting and shut down."""
        self.shutdown_event.set()


async def main():
    """Run a bot session using .env config (CLI mode)."""
    cfg = Config()
    session = BotSession()
    await session.start(meeting_url=cfg.TEAMS_MEETING_URL, bot_name=cfg.BOT_NAME)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
