"""Vision Observer: periodic screenshot analysis via Gemini + DG UpdateThink."""

import asyncio
import logging

from google import genai
from google.genai import types

logger = logging.getLogger("teams_agent.vision_observer")

VISION_PROMPT = (
    "Analyze this Microsoft Teams meeting screenshot. Identify:\n"
    "1. Who is currently speaking (look for the highlighted name or active speaker indicator)\n"
    "2. Any shared screen content (PowerPoint slides, documents, screen shares) — "
    "summarize the visible text and key points\n"
    "3. The number of visible participants\n\n"
    "Be concise. Format as:\n"
    "SPEAKER: [name or 'unknown']\n"
    "SHARED_CONTENT: [description or 'none']\n"
    "PARTICIPANTS: [count]"
)


class VisionObserver:
    """Periodic screenshot -> Gemini vision -> DG UpdateThink."""

    def __init__(
        self,
        *,
        page,
        dg_processor,
        google_api_key: str,
        gemini_model: str = "gemini-2.5-flash-lite-preview-06-17",
        think_model: str = "gpt-4o-mini",
        interval: float = 10.0,
        base_prompt: str = "",
    ):
        self._page = page
        self._dg_processor = dg_processor
        self._genai_client = genai.Client(api_key=google_api_key)
        self._gemini_model = gemini_model
        self._think_model = think_model
        self._interval = interval
        self._base_prompt = base_prompt
        self._running = False
        self._task: asyncio.Task | None = None
        self._visual_history: list[str] = []  # Last 2 visual contexts
        self._last_visual_context = ""
        self._consecutive_failures = 0
        self._MAX_FAILURES = 5

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._observe_loop())
        logger.info("VisionObserver started (interval=%ds, model=%s)", self._interval, self._gemini_model)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("VisionObserver stopped")

    async def _observe_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break

                # Only proceed if DG socket is connected
                if not self._dg_processor._socket or not self._dg_processor._ready:
                    continue

                try:
                    visual_context = await self._capture_and_analyze()
                    logger.info("Vision analysis: %s", visual_context.replace('\n', ' | '))
                    if visual_context and visual_context != self._last_visual_context:
                        self._visual_history.append(visual_context)
                        self._visual_history = self._visual_history[-2:]
                        self._last_visual_context = visual_context
                        await self._send_update_think()
                    self._consecutive_failures = 0
                except Exception as e:
                    self._consecutive_failures += 1
                    logger.warning(
                        "VisionObserver error (%d/%d): %s",
                        self._consecutive_failures,
                        self._MAX_FAILURES,
                        e,
                    )
                    if self._consecutive_failures >= self._MAX_FAILURES:
                        logger.error("VisionObserver: too many failures, stopping")
                        self._running = False
                        break
        except asyncio.CancelledError:
            pass

    async def _capture_and_analyze(self) -> str:
        # Dismiss any camera/mic permission popups before capturing
        try:
            btn = self._page.locator(
                '[data-tid="callingAlertDismissButton_VideoCapturePermissionDenied"], '
                '[data-tid="callingAlertDismissButton_AudioCapturePermissionDenied"]'
            )
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click()
                logger.info("Dismissed permission notification before capture")
                await asyncio.sleep(0.5)
        except Exception:
            pass

        screenshot_bytes = await self._page.screenshot(type="png")

        response = await self._genai_client.aio.models.generate_content(
            model=self._gemini_model,
            contents=[
                VISION_PROMPT,
                types.Part(inline_data=types.Blob(data=screenshot_bytes, mime_type="image/png")),
            ],
        )
        return response.text if response.text else ""

    async def _send_update_think(self) -> None:
        """Update DG Voice Agent system prompt with last 2 visual contexts.

        Visual context goes FIRST so Deepgram's prompt truncation (if any)
        cuts the base prompt tail, not the fresh visual data.
        """
        vision_parts = []
        for i, ctx in enumerate(self._visual_history):
            label = "CURRENT" if i == len(self._visual_history) - 1 else "PREVIOUS"
            vision_parts.append(f"[{label}] {ctx}")
        vision_block = "\n".join(vision_parts)

        updated_prompt = (
            f"=== VISUAL CONTEXT (auto-updated, last 2 snapshots) ===\n"
            f"{vision_block}\n"
            f"=== END VISUAL CONTEXT ===\n\n"
            f"{self._base_prompt}"
        )

        try:
            from deepgram.agent import AgentV1UpdatePrompt

            await self._dg_processor._socket.send_update_prompt(
                AgentV1UpdatePrompt(prompt=updated_prompt)
            )
            logger.info("Vision context sent via UpdatePrompt")
        except Exception as e:
            logger.warning("Failed to update DG prompt: %s", e)

    @property
    def last_visual_context(self) -> str:
        return self._last_visual_context
