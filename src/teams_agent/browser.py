"""Playwright automation: join a Microsoft Teams meeting via Chromium.

Launches Chrome as a subprocess with --remote-debugging-port (TCP-based CDP)
instead of Playwright's default --remote-debugging-pipe, which crashes on
Windows Server 2025. Connects via connect_over_cdp().

Injects audio_bridge.js to intercept getUserMedia and RTCPeerConnection
for driverless audio routing over WebSocket.

NOTE: Teams DOM selectors are fragile and may break when Microsoft updates
the Teams web client. Use `playwright codegen <meeting_url>` to discover
current selectors if the join flow fails.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile

from playwright.async_api import Browser, Page, async_playwright

logger = logging.getLogger("teams_agent.browser")

# Project root
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

# Path to the JS audio bridge
BRIDGE_JS_PATH = os.path.join(os.path.dirname(__file__), "js", "audio_bridge.js")

CDP_PORT = 9222

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

CHROME_ARGS = [
    "--headless",
    "--disable-gpu",
    "--no-sandbox",
    f"--remote-debugging-port={CDP_PORT}",
    "--use-fake-ui-for-media-stream",
    # NOTE: --auto-accept-camera-and-microphone-capture crashes Chrome on Win Server 2025
    "--disable-features=WebRtcHideLocalIpsWithMdns",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    # Allow wss:// with self-signed cert from HTTPS pages
    "--allow-insecure-localhost",
    "--allow-running-insecure-content",
    "--disable-web-security",
    "--ignore-certificate-errors",
]


def _find_chrome() -> str:
    """Find an installed Chrome or Edge binary."""
    for path in CHROME_PATHS:
        if os.path.isfile(path):
            return path
    # Fallback: try PATH
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError("Chrome or Edge not found. Install Google Chrome.")


async def join_teams_meeting(meeting_url: str, bot_name: str) -> tuple[Browser, Page, None]:
    """Join a Teams meeting and return (browser, page, None) handles.

    The browser and page must be kept alive for the duration of the meeting.
    """
    os.makedirs(os.path.join(_PROJECT_ROOT, "logs"), exist_ok=True)
    chrome_path = _find_chrome()
    # Use a simple path (no short-name like ADMINI~1) — Chrome crashes with 8.3 paths
    user_data_dir = r"C:\temp\chrome_bot_profile"
    if os.path.exists(user_data_dir):
        shutil.rmtree(user_data_dir, ignore_errors=True)
    os.makedirs(user_data_dir, exist_ok=True)

    # Launch Chrome with DETACHED_PROCESS — standard Popen with pipe inheritance
    # crashes Chrome on Windows Server 2025 (STATUS_BREAKPOINT).
    DETACHED_PROCESS = 0x00000008
    chrome_proc = subprocess.Popen(
        [chrome_path, *CHROME_ARGS, f"--user-data-dir={user_data_dir}"],
        creationflags=DETACHED_PROCESS,
        close_fds=True,
    )
    logger.info("Chrome launched (PID=%d), waiting for CDP on port %d...", chrome_proc.pid, CDP_PORT)

    # Wait for CDP port to be ready
    for _ in range(20):
        await asyncio.sleep(0.5)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", CDP_PORT)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            continue
    else:
        chrome_proc.terminate()
        raise RuntimeError(f"Chrome CDP port {CDP_PORT} not ready after 10 seconds")

    # Connect Playwright via CDP
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
    logger.info("Connected to Chrome via CDP")

    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    # Grant mic/camera permissions on the Teams origins
    for origin in ["https://teams.live.com", "https://teams.microsoft.com"]:
        await context.grant_permissions(["microphone", "camera"], origin=origin)
    logger.info("Granted microphone/camera permissions for Teams origins")

    # Load audio bridge JS source
    with open(BRIDGE_JS_PATH) as f:
        bridge_js = f.read()

    page = await context.new_page()

    # Inject audio bridge via CDP AND route-based HTML injection.
    # CDP addScriptToEvaluateOnNewDocument injects into EVERY new document
    # (including Teams' SPA navigations that create new contexts).
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": bridge_js})

    # Also listen for frame navigations and re-inject via CDP
    async def _on_frame_navigated(frame):
        try:
            await frame.evaluate(bridge_js)
            logger.debug("Re-injected audio_bridge.js after frame navigation")
        except Exception:
            pass

    page.on("framenavigated", lambda frame: asyncio.create_task(_on_frame_navigated(frame)))
    logger.info("Injected audio_bridge.js via CDP (runs before page JS)")

    logger.info("Navigating to Teams meeting: %s", meeting_url)
    await page.goto(meeting_url, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)  # Let Teams JS initialize

    # Step 1: Click "Continue on this browser" / "Join on the web"
    logger.info("Looking for 'Continue on this browser' button...")
    continue_btn = page.locator(
        'button:has-text("Continue on this browser"), '
        'a:has-text("Continue on this browser"), '
        'button:has-text("Join on the web"), '
        'a:has-text("Join on the web")'
    )
    await continue_btn.first.click(timeout=30000)
    logger.info("Clicked continue/join-on-web button.")

    # Wait for pre-join screen to fully render
    logger.info("Waiting for pre-join screen to load...")
    join_btn = page.locator(
        'button:has-text("Join now"), '
        'button:has-text("Join meeting"), '
        'button:has-text("Join")'
    )

    try:
        await join_btn.first.wait_for(state="visible", timeout=45000)
    except Exception:
        await page.screenshot(path=os.path.join(_PROJECT_ROOT, "logs", "debug_join_timeout.png"))
        logger.error("Timed out waiting for Join button — see debug_join_timeout.png")
        raise
    logger.info("Pre-join screen loaded")

    # Step 2: Dismiss permissions dialog if it blocks the pre-join screen.
    # Chrome doesn't grant real mic permissions in headless/CDP mode, so this
    # dialog always appears. We dismiss it and enable audio after joining.
    no_av_btn = page.locator('button:has-text("Continue without audio or video")')
    if await no_av_btn.count() > 0 and await no_av_btn.first.is_visible():
        logger.info("Permissions dialog present — dismissing to join without audio initially")
        await no_av_btn.first.click()
        await asyncio.sleep(2)
        await join_btn.first.wait_for(state="visible", timeout=15000)

    # Step 3: Fill in bot name
    try:
        name_input = page.locator('input[placeholder*="name" i]')
        await name_input.first.wait_for(state="visible", timeout=5000)
        await name_input.first.fill(bot_name)
        logger.info("Entered bot name: %s", bot_name)
    except Exception:
        logger.debug("Name input not found, continuing.")

    # Screenshot for debugging
    await page.screenshot(path=os.path.join(_PROJECT_ROOT, "logs", "debug_prejoin.png"))
    logger.info("Pre-join screenshot saved")

    # Step 4: Click "Join now"
    logger.info("Clicking 'Join now'...")
    await join_btn.first.wait_for(state="visible", timeout=15000)
    await join_btn.first.click(timeout=15000)

    # Step 5: Wait for meeting — bot may sit in lobby waiting for admission
    logger.info("Waiting to enter meeting (may be in lobby)...")

    # Take a screenshot right after clicking Join
    await asyncio.sleep(3)
    await page.screenshot(path=os.path.join(_PROJECT_ROOT, "logs", "debug_joining.png"))
    logger.info("Joining screenshot saved")

    # Wait up to 2 minutes for in-meeting indicators (lobby wait included)
    in_meeting = page.locator(
        'button[aria-label*="Leave" i], '
        'button[aria-label*="Unmute" i], '
        '[data-tid="hangup-button"], '
        '[data-tid="toggle-mute"]'
    )
    try:
        await in_meeting.first.wait_for(state="visible", timeout=60000)
        logger.info("Successfully joined the Teams meeting.")
    except Exception:
        await page.screenshot(path=os.path.join(_PROJECT_ROOT, "logs", "debug_waiting.png"))
        logger.error("Timed out waiting to enter meeting — see debug_waiting.png")
        raise

    # Step 5b: Dismiss camera/mic permission notification banner
    await asyncio.sleep(2)
    try:
        dismiss_btn = page.locator(
            '[data-tid="callingAlertDismissButton_VideoCapturePermissionDenied"]'
        )
        if await dismiss_btn.count() > 0 and await dismiss_btn.first.is_visible():
            await dismiss_btn.first.click()
            logger.info("Dismissed camera permission notification")
            await asyncio.sleep(1)
    except Exception:
        pass

    # Step 6: Inject audio bridge JS directly into the meeting page.
    # CDP addScriptToEvaluateOnNewDocument doesn't persist through Teams' SPA nav,
    # so we must inject via page.evaluate() after the meeting page is loaded.
    await asyncio.sleep(2)
    await page.evaluate(bridge_js)
    logger.info("Injected audio_bridge.js into meeting page via page.evaluate")

    bridge_state = await page.evaluate("""() => ({
        gumOverridden: !navigator.mediaDevices.getUserMedia.toString().includes('native code'),
        rtcOverridden: !RTCPeerConnection.toString().includes('native code'),
    })""")
    logger.info("Audio bridge state: %s", bridge_state)

    # Step 7: Try to unmute — triggers getUserMedia which our JS overrides
    await asyncio.sleep(2)
    try:
        unmute_btn = page.locator(
            'button[aria-label*="Unmute" i], '
            '[data-tid="toggle-mute"]'
        )
        if await unmute_btn.count() > 0:
            await unmute_btn.first.click(timeout=5000)
            logger.info("Clicked unmute — getUserMedia override should activate")
            await asyncio.sleep(1)
    except Exception:
        logger.debug("Unmute button not found.")

    return browser, page, chrome_proc


async def setup_audio_capture(page):
    """Set up audio capture and track injection. Call AFTER bridge.start().

    This must run after expose_function('sendAudioToPython') so captured
    audio immediately reaches the bridge instead of being dropped.
    """
    bridge_js_path = BRIDGE_JS_PATH
    with open(bridge_js_path) as f:
        bridge_js = f.read()

    # Step 8: Capture audio from existing <audio>/<video> elements.
    # Teams sets up WebRTC before our JS override, so the RTC hook won't
    # catch existing tracks. We capture directly from the DOM audio elements.
    await asyncio.sleep(2)
    capture_result = await page.evaluate("""() => {
        const audioEls = document.querySelectorAll('audio, video');
        let captured = false;
        for (const el of audioEls) {
            if (el.srcObject && el.srcObject.getAudioTracks().length > 0) {
                // Use the shared single-capture function (prevents duplicates)
                captured = window.__pipecatStartCapture(el.srcObject);
                if (captured) break;  // Only capture once
            }
        }
        return {audioElements: audioEls.length, captured: captured, alreadyActive: window.__pipecatCaptureActive};
    }""")
    logger.info("Audio element capture: %s", capture_result)

    # Step 9: Inject our virtual mic into the existing RTCPeerConnection.
    # Since we joined without audio, Teams never called getUserMedia, so
    # the virtual mic MediaStream isn't connected to WebRTC. We create it
    # now and use replaceTrack to inject it into the active RTC sender.
    await asyncio.sleep(2)
    inject_result = await page.evaluate("""async () => {
        try {
            // Create the virtual mic stream by calling our getUserMedia override
            const stream = await navigator.mediaDevices.getUserMedia({audio: true});
            if (!stream || stream.getAudioTracks().length === 0) {
                return {error: 'No audio tracks from getUserMedia'};
            }
            const ourTrack = stream.getAudioTracks()[0];

            // Find ALL RTCPeerConnections and inject our track
            // First check stored PCs from our hook
            let pcs = window.__pipecatPCs || [];

            // Also try to find PCs via getStats on the page
            let injected = 0;
            for (const pc of pcs) {
                if (pc.connectionState === 'closed') continue;
                const senders = pc.getSenders();
                for (const sender of senders) {
                    if (sender.track && sender.track.kind === 'audio') {
                        await sender.replaceTrack(ourTrack);
                        injected++;
                    }
                }
                // If no audio sender exists, add one
                if (!senders.some(s => s.track && s.track.kind === 'audio')) {
                    try {
                        pc.addTrack(ourTrack, stream);
                        injected++;
                    } catch(e) {}
                }
            }

            return {
                pcs: pcs.length,
                injected: injected,
                hasPlayer: !!window.__pipecatPlayer,
                trackLabel: ourTrack.label,
            };
        } catch(e) {
            return {error: e.name + ': ' + e.message};
        }
    }""")
    logger.info("Audio track injection: %s", inject_result)


async def monitor_meeting(page: Page, shutdown_event: asyncio.Event):
    """Watch for meeting-end signals and set shutdown_event when detected."""
    logger.info("Meeting monitor started.")
    consecutive_failures = 0

    while not shutdown_event.is_set():
        try:
            # Check for end/removal indicators (text + Rejoin button)
            ended = page.locator(
                'text="The meeting has ended", '
                'text="You\'ve been removed", '
                'text="You were removed from the meeting", '
                'text="You\'ve been removed from this meeting", '
                '[data-tid="meeting-ended"], '
                '[data-tid="calling-retry-rejoinbutton"]'
            )
            if await ended.count() > 0:
                logger.info("Meeting ended or bot was removed — shutting down.")
                shutdown_event.set()
                return

            # Also check if the Leave button disappeared (meeting ended unexpectedly)
            leave_btn = page.locator(
                'button[aria-label*="Leave" i], [data-tid="hangup-button"]'
            )
            if await leave_btn.count() == 0:
                # No leave button — might be on a post-meeting page
                # Check if Rejoin or Dismiss is visible
                post_meeting = page.locator(
                    'button:has-text("Rejoin"), button:has-text("Dismiss")'
                )
                if await post_meeting.count() > 0:
                    logger.info("Post-meeting screen detected — shutting down.")
                    shutdown_event.set()
                    return

            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                logger.warning("Meeting monitor: %d consecutive check failures, assuming disconnected.", consecutive_failures)
                shutdown_event.set()
                return

        await asyncio.sleep(5)


async def leave_meeting(page: Page, browser: Browser, _chrome_proc=None):
    """Click the Leave button and close everything."""
    try:
        leave_btn = page.locator(
            'button[data-tid="hangup-button"], '
            'button[aria-label*="Leave" i]'
        )
        if await leave_btn.count() > 0:
            await leave_btn.first.click(timeout=5000)
            logger.info("Clicked Leave button.")
            await asyncio.sleep(1)
    except Exception:
        logger.debug("Could not click Leave button, closing browser directly.")

    try:
        await browser.close()
    except Exception:
        pass
    logger.info("Browser closed.")

    # Kill browser processes launched for this bot (Chrome or Edge)
    os.system("taskkill /F /IM chrome.exe >nul 2>&1")
    os.system("taskkill /F /IM msedge.exe >nul 2>&1")
    logger.info("Browser processes terminated.")
