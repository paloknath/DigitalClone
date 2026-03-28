"""FastAPI server for launching Teams Audio Agent bots via REST API.

Usage:
    python api_server.py
    uvicorn api_server:app --host 0.0.0.0 --port 8000

Endpoints:
    POST /bot/start   — Start a bot in a Teams meeting
    POST /bot/stop    — Stop the active bot session
    GET  /bot/status  — Get current bot status
"""

import asyncio
import logging
import subprocess
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.teams_agent.__main__ import BotSession
from src.teams_agent.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("teams_agent.api")

app = FastAPI(
    title="Teams Audio Agent API",
    description="Launch AI voice bots into Microsoft Teams meetings",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single active session (one bot at a time on this server)
_session: BotSession | None = None
_session_id: str | None = None
_session_task: asyncio.Task | None = None


class StartRequest(BaseModel):
    meeting_url: str
    bot_name: str = "AI Assistant"
    vision_enabled: bool | None = None  # None = use .env VISION_ENABLED


class VisionRequest(BaseModel):
    enabled: bool


class StartResponse(BaseModel):
    session_id: str
    status: str
    message: str


class StatusResponse(BaseModel):
    session_id: str | None
    status: str
    message: str
    vision_active: bool = False


def _kill_port_holders(port: int):
    """Kill any process listening on the given TCP port (Windows)."""
    try:
        result = subprocess.run(
            ["netstat", "-aon"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid > 0:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                        timeout=5,
                    )
                    logger.info("Killed process %d holding port %d", pid, port)
    except Exception as e:
        logger.warning("Failed to kill port %d holders: %s", port, e)


async def _cleanup_previous_session():
    """Force-stop any existing bot session and release OS resources."""
    global _session, _session_id, _session_task

    if _session:
        logger.info("Cleaning up previous session (status=%s)...", _session.status)
        _session.shutdown_event.set()

        if _session_task and not _session_task.done():
            _session_task.cancel()
            try:
                await asyncio.wait_for(_session_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        _session = None
        _session_id = None
        _session_task = None

    # Kill anything still holding the WSS port
    cfg = Config()
    _kill_port_holders(cfg.WS_PORT)
    # Brief pause to let the OS release the socket
    await asyncio.sleep(1)


@app.post("/bot/start", response_model=StartResponse)
async def start_bot(req: StartRequest):
    """Start a bot session in the given Teams meeting.

    Returns once the bot has joined the meeting, audio pipeline is active,
    and the bot is ready to listen and respond.
    """
    global _session, _session_id, _session_task

    # Force-cleanup any previous session (crashed or still running)
    await _cleanup_previous_session()

    _session = BotSession()
    _session_id = uuid.uuid4().hex[:12]
    ready_event = asyncio.Event()

    async def on_ready():
        ready_event.set()

    async def run_session():
        try:
            await _session.start(
                meeting_url=req.meeting_url,
                bot_name=req.bot_name,
                ready_callback=on_ready,
                vision_enabled=req.vision_enabled,
            )
        except Exception:
            logger.exception("Bot session crashed")
            _session.status = "stopped"

    # Launch session in background
    _session_task = asyncio.create_task(run_session())

    # Wait for the bot to be fully ready (joined + pipeline + audio)
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Bot failed to become ready within 60s (status={_session.status})",
        )

    return StartResponse(
        session_id=_session_id,
        status=_session.status,
        message=f"Bot '{req.bot_name}' joined and ready in meeting",
    )


@app.post("/bot/stop", response_model=StatusResponse)
async def stop_bot():
    """Stop the active bot session."""
    if not _session or _session.status == "stopped":
        raise HTTPException(status_code=404, detail="No active bot session")

    await _session.stop()
    # Give it a moment to clean up
    if _session_task:
        try:
            await asyncio.wait_for(_session_task, timeout=10)
        except asyncio.TimeoutError:
            pass

    return StatusResponse(
        session_id=_session_id,
        status="stopped",
        message="Bot session stopped",
        vision_active=False,
    )


@app.get("/bot/status", response_model=StatusResponse)
async def get_status():
    """Get the current bot session status."""
    if not _session:
        return StatusResponse(
            session_id=None,
            status="idle",
            message="No bot session",
        )

    return StatusResponse(
        session_id=_session_id,
        status=_session.status,
        message=f"Bot session {_session_id}",
        vision_active=_session.vision_active,
    )


@app.post("/bot/vision", response_model=StatusResponse)
async def toggle_vision(req: VisionRequest):
    """Enable or disable vision sharing on the active bot session."""
    if not _session or _session.status not in ("ready", "running"):
        raise HTTPException(status_code=404, detail="No active bot session")

    if req.enabled:
        success = await _session.start_vision()
        msg = "Vision observer started" if success else "Failed to start vision observer"
    else:
        success = await _session.stop_vision()
        msg = "Vision observer stopped" if success else "Failed to stop vision observer"

    if not success:
        raise HTTPException(status_code=500, detail=msg)

    return StatusResponse(
        session_id=_session_id,
        status=_session.status,
        message=msg,
        vision_active=_session.vision_active,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=6789)
