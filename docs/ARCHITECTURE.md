# Architecture

## System Overview

Teams Voice Agent is a real-time AI voice bot that joins Microsoft Teams meetings through browser automation and participates in conversations using cloud AI services. The system uses a driverless architecture -- no virtual audio cables or OS-level audio drivers are needed.

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLOUD AI SERVICES                        │
│                                                                 │
│   Google Gemini Live API          Deepgram Voice Agent API      │
│   (native speech-to-speech)       (STT + LLM + TTS pipeline)   │
│                                                                 │
│   Gemini Vision API                                             │
│   (screenshot analysis)                                         │
└──────────────────────────────┬──────────────────────────────────┘
                               │ WebSocket / HTTPS
                               │
┌──────────────────────────────┴──────────────────────────────────┐
│                     PIPECAT PIPELINE (Python)                   │
│                                                                 │
│   WebSocket Server (TLS, port 8765)                             │
│   ├── InputAudioRawFrame  → Cloud AI                            │
│   └── OutputAudioRawFrame ← Cloud AI                            │
│                                                                 │
│   Frame Processors:                                             │
│   ├── Google S2S: GeminiMultimodalLiveLLMService                │
│   └── Deepgram:  DeepgramVoiceAgentProcessor (custom)           │
└──────────────────────────────┬──────────────────────────────────┘
                               │ TLS WebSocket (raw PCM binary)
                               │
┌──────────────────────────────┴──────────────────────────────────┐
│                     AUDIO BRIDGE (Python)                       │
│                                                                 │
│   WebSocket Client → Pipecat Pipeline                           │
│   ├── Send loop: browser audio → pipeline (60ms chunks, 16kHz) │
│   ├── Receive loop: pipeline audio → browser (24kHz)            │
│   ├── Interruption detection (energy-based barge-in)            │
│   └── Echo gate coordination with browser JS                    │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Playwright expose_function / evaluate
                               │
┌──────────────────────────────┴──────────────────────────────────┐
│                   BROWSER (Chrome/Edge, Headless)                │
│                                                                 │
│   audio_bridge.js (injected into Teams page):                   │
│   ├── getUserMedia override → virtual mic (bot mouth)           │
│   ├── RTCPeerConnection hook → capture remote audio (bot ears)  │
│   ├── Playback buffer: 24kHz PCM → upsampled to 48kHz          │
│   └── Echo gate: mutes capture during bot speech                │
│                                                                 │
│   Teams Web Client:                                             │
│   ├── WebRTC peer connections (meeting audio)                   │
│   └── Meeting UI (for vision observer screenshots)              │
└─────────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. Browser Automation (`browser.py`)

Launches Chrome or Edge in headless mode with CDP (Chrome DevTools Protocol) and joins a Teams meeting through the web client.

**Key decisions:**
- Uses `--remote-debugging-port` (TCP-based CDP) instead of Playwright's default `--remote-debugging-pipe`, which crashes on Windows Server 2025
- Injects `audio_bridge.js` via CDP `Page.addScriptToEvaluateOnNewDocument` to ensure it runs before Teams' own JavaScript
- Handles the full Teams join flow: "Continue on this browser" -> name entry -> "Join now" -> lobby wait -> meeting entry
- Dismissed permission dialogs (camera/mic) since headless Chrome can't grant real device access

### 2. Audio Bridge JS (`js/audio_bridge.js`)

Injected into the Teams page to intercept all audio I/O without any OS-level audio drivers.

**Bot Mouth (playback):**
- Overrides `navigator.mediaDevices.getUserMedia` to return a virtual `MediaStream`
- A `ScriptProcessor` node reads from a playback buffer and feeds audio into the virtual mic track
- Incoming bot audio (24kHz PCM from Python) is upsampled 2x to 48kHz via linear interpolation
- The virtual mic track is injected into Teams' `RTCPeerConnection` via `replaceTrack()`

**Bot Ears (capture):**
- Hooks `RTCPeerConnection` to capture remote audio tracks (meeting participants speaking)
- Captures from existing `<audio>` / `<video>` elements as a fallback
- Decimates 48kHz capture down to 16kHz (3x) for the pipeline
- Sends PCM audio to Python via `sendAudioToPython` (Playwright `expose_function`)
- Single-instance guard prevents duplicate capture streams

**Echo Gate:**
- When the bot is playing audio (`window.botIsSpeaking = true`), capture is fully muted
- If the user speaks loudly during bot output (energy > threshold), the playback buffer is cleared immediately (barge-in) and capture resumes at full volume
- Echo gate timeout: 150ms after last bot audio chunk

### 3. Audio Bridge Python (`bridge.py`)

Connects to the Pipecat pipeline's WebSocket server and manages bidirectional audio flow.

**Send path (browser -> pipeline):**
- Accumulates audio from browser callbacks into a byte buffer
- Sends 1920-byte chunks (60ms at 16kHz) at 60ms intervals
- Energy-based speech detection (threshold: 100 RMS)
- Interruption detection: if user energy > 300 while bot is outputting, sends a 4-byte interrupt marker
- Turn-end silence padding: after 300ms of silence following speech, sends 3 silence chunks to help STT finalize

**Receive path (pipeline -> browser):**
- Receives raw PCM bytes from the pipeline
- Base64-encodes and pushes to browser via `page.evaluate("window.__playAudioFromPython(...)")`
- Tracks bot output state for echo gate coordination (auto-expires after 500ms)

### 4. Pipecat Pipelines

#### Google S2S (`pipeline.py`)

Uses Pipecat's built-in `GeminiMultimodalLiveLLMService` for native speech-to-speech with Gemini Live API. Audio goes in, audio comes out -- no separate STT/LLM/TTS steps.

#### Deepgram Voice Agent (`pipeline_deepgram.py`)

Custom `DeepgramVoiceAgentProcessor` (a Pipecat `FrameProcessor`) that wraps Deepgram's Voice Agent WebSocket API.

**Configuration sent to Deepgram:**
- **Listen**: Flux STT model with end-of-turn detection (<300ms)
- **Think**: OpenAI LLM (GPT-4o-mini by default, configurable)
- **Speak**: Cartesia TTS (Sonic 2 model, configurable voice ID)

**Event handling:**
- `AgentV1ConversationText` -- logs transcript, feeds to TranscriptCollector
- `AgentV1UserStartedSpeaking` -- triggers barge-in if bot is speaking
- `AgentV1AgentStartedSpeaking` / `AgentV1AgentAudioDone` -- tracks bot speaking state
- Binary messages -- raw PCM audio, pushed as `OutputAudioRawFrame`

**Lazy connection**: DG session is established on first audio frame (not on `StartFrame`) to avoid Deepgram's 10-second inactivity timeout.

### 5. Vision Observer (`vision_observer.py`)

Periodic screenshot analysis for visual awareness (Deepgram pipeline only).

**Flow:**
1. Every N seconds (configurable, default 10s), takes a screenshot of the Teams page
2. Sends the screenshot to Gemini vision model for analysis
3. Gemini returns structured text: speaker name, shared content description, participant count
4. If the analysis changed, updates the Deepgram agent's system prompt via `AgentV1UpdatePrompt`

**Prompt structure:**
```
=== VISUAL CONTEXT (auto-updated, last 2 snapshots) ===
[PREVIOUS] SPEAKER: ... | SHARED_CONTENT: ... | PARTICIPANTS: ...
[CURRENT] SPEAKER: ... | SHARED_CONTENT: ... | PARTICIPANTS: ...
=== END VISUAL CONTEXT ===

{base persona prompt}
```

Visual context goes first in the prompt so Deepgram's token limit truncation (if any) cuts the persona tail, not the fresh visual data. Only the last 2 snapshots are kept to prevent prompt growth.

### 6. Meeting Memory (`meeting_memory.py`)

Cross-meeting context retrieval using ChromaDB vector store and Gemini summaries.

**Store (after meeting ends):**
1. TranscriptCollector provides formatted transcript
2. Gemini generates a meeting summary (key topics, decisions, action items)
3. Summary + metadata (URL, timestamp, first 5000 chars of transcript) stored in ChromaDB
4. Oldest meetings evicted when count exceeds 20

**Retrieve (before meeting starts):**
1. Cosine similarity search across all stored meetings
2. Top 3 most relevant past meeting summaries injected into system prompt as "PAST MEETING CONTEXT"

### 7. BotSession Orchestrator (`__main__.py`)

Manages the full lifecycle of a bot session:

```
idle → starting → joined → ready → running → stopped
```

1. Load config, retrieve past meeting context from ChromaDB
2. Start Pipecat pipeline (WebSocket server)
3. Launch browser, join Teams meeting
4. Start AudioBridge (WebSocket client connects to pipeline)
5. Set up audio capture and track injection
6. Optionally start VisionObserver
7. Run pipeline + meeting monitor concurrently
8. On shutdown: stop vision, generate summary, store in ChromaDB, leave meeting, close browser

### 8. REST API (`api_server.py`)

FastAPI server for external control of bot sessions.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/bot/start` | POST | Start a bot (meeting_url, bot_name, vision_enabled) |
| `/bot/stop` | POST | Stop active bot session |
| `/bot/status` | GET | Current session status + vision state |
| `/bot/vision` | POST | Toggle vision observer on/off mid-session |

Single-session model: one bot at a time per server instance. Starting a new session force-cleans the previous one.

## Audio Format Reference

| Segment | Sample Rate | Channels | Encoding | Chunk Size |
|---------|-------------|----------|----------|------------|
| Browser capture | 48kHz | 1 | Float32 | 1024 samples |
| After decimation (to Python) | 16kHz | 1 | Int16 LE | ~341 samples |
| Bridge -> Pipeline | 16kHz | 1 | Int16 LE | 1920 bytes (60ms) |
| Pipeline -> Bridge (Deepgram) | 24kHz | 1 | Int16 LE | variable |
| Bridge -> Browser | 24kHz | 1 | Int16 LE (base64) | variable |
| Browser playback | 48kHz | 1 | Float32 | upsampled 2x |

## Key Design Decisions

1. **Driverless audio**: JavaScript MediaStream interception eliminates the need for VB-Cable or virtual audio devices, making deployment simpler and more portable.

2. **TLS WebSocket**: The Pipecat pipeline uses a self-signed TLS WebSocket server because Chrome's content security policy blocks `ws://` connections from `https://` pages (Teams).

3. **Lazy DG connection**: The Deepgram Voice Agent connection is established on first audio frame rather than pipeline start, avoiding Deepgram's inactivity timeout.

4. **Echo suppression via full mute**: During bot speech, captured audio is completely zeroed out (not attenuated) since Deepgram handles VAD server-side. Interruption detection still works via energy threshold check before muting.

5. **CDP over pipe**: Chrome is launched with `--remote-debugging-port` (TCP) instead of Playwright's default pipe mode, which causes STATUS_BREAKPOINT crashes on Windows Server 2025.

6. **Prompt management**: Vision context is placed at the start of UpdatePrompt payloads and limited to 2 snapshots, preventing prompt growth and ensuring truncation doesn't cut visual data.
