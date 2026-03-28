# DigitalClone

> **DISCLAIMER:** This project is a **Proof of Concept (PoC)** for educational and research purposes only. It is not an official Microsoft product and is not affiliated with, endorsed by, or sponsored by Microsoft Corporation. "Microsoft Teams" is a trademark of Microsoft Corporation. Use of this tool to automate Microsoft Teams is subject to the [Microsoft Services Agreement](https://www.microsoft.com/en-us/servicesagreement). The author assumes no liability for misuse.

An AI-powered voice agent that joins Microsoft Teams meetings and participates in real-time conversations. Built as a learning project to explore real-time S2S architectures, multimodal RAG, and vision-led browser automation. Uses Playwright with a driverless WebSocket audio bridge -- no virtual audio drivers needed.

## Features

- **Real-time voice conversations** in Teams meetings via two AI pipeline options:
  - **Google Gemini S2S** -- native speech-to-speech via Gemini Live API
  - **Deepgram Voice Agent** -- Deepgram STT + OpenAI LLM + Cartesia TTS
- **Driverless audio** -- pure JavaScript MediaStream interception, no VB-Cable or virtual audio devices
- **Visual awareness** -- periodic screenshot analysis via Gemini vision, injected into conversation context
- **Meeting memory** -- ChromaDB-backed cross-meeting context retrieval with Gemini summaries
- **Barge-in support** -- interrupt the bot mid-sentence and it stops to listen
- **REST API** -- start/stop bots and toggle features via HTTP endpoints
- **Configurable persona** -- customize the bot's personality via file or environment variable

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed architecture documentation.

**High-level flow:**

```
Teams (Browser)  <-->  Audio Bridge (Python)  <-->  Pipecat Pipeline  <-->  Cloud AI
     JS intercept         WebSocket client          WebSocket server       Gemini / Deepgram
     capture/playback     PCM serialization         frame processing       STT + LLM + TTS
```

## Prerequisites

- **Python 3.11+**
- **Windows** (tested on Windows Server 2025; Linux support is possible with modifications)
- **Google Chrome** or **Microsoft Edge**
- **API Keys** for your chosen pipeline:
  - Google Gemini API key (for Google S2S pipeline and/or vision observer)
  - Deepgram API key (for Deepgram pipeline)
  - Cartesia voice ID (for Deepgram pipeline -- browse voices at [play.cartesia.ai](https://play.cartesia.ai/))

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/paloknath/DigitalClone.git
cd DigitalClone
pip install -r requirements.txt
playwright install chromium
```

### 2. Generate TLS certificates

The audio bridge requires TLS for secure WebSocket connections:

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 \
  -keyout certs/localhost-key.pem \
  -out certs/localhost.pem \
  -days 365 -nodes -subj "/CN=localhost"
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys, meeting URL, and preferences
```

Key settings in `.env`:

| Variable | Description |
|----------|-------------|
| `PIPELINE_MODE` | `google_s2s` or `deepgram_voice_agent` |
| `TEAMS_MEETING_URL` | Teams meeting link to join |
| `BOT_NAME` | Display name in the meeting |
| `GOOGLE_API_KEY` | Google Gemini API key |
| `DEEPGRAM_API_KEY` | Deepgram API key |
| `CARTESIA_VOICE_ID` | Cartesia voice for TTS |
| `VISION_ENABLED` | Enable visual awareness (`true`/`false`) |

### 4. Run

**CLI mode** (single meeting):

```bash
python run.py
```

**API server mode** (control via REST):

```bash
# Windows
start_api.bat

# Or directly
python api_server.py
```

## REST API

The API server runs on port `6789` by default.

### Start a bot

```bash
curl -X POST http://localhost:6789/bot/start \
  -H "Content-Type: application/json" \
  -d '{"meeting_url": "https://teams.live.com/meet/...", "bot_name": "AI Assistant", "vision_enabled": true}'
```

### Stop the bot

```bash
curl -X POST http://localhost:6789/bot/stop
```

### Check status

```bash
curl http://localhost:6789/bot/status
```

### Toggle vision

```bash
curl -X POST http://localhost:6789/bot/vision \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

## Persona Customization

The bot's personality is fully configurable. Behavioral rules (how to sound human, response rules, visual awareness) are always applied automatically.

**Option 1: Persona file** (recommended for longer prompts)

```bash
cp persona.txt.example persona.txt
# Edit persona.txt with your custom personality
```

Then set in `.env`:

```
PERSONA_FILE=persona.txt
```

**Option 2: Inline in .env**

```
SYSTEM_INSTRUCTION=You are a helpful engineering lead named Alex. You speak casually and focus on practical solutions.
```

**Option 3: Default**

Leave both empty for a generic friendly AI participant.

Use `{bot_name}` as a placeholder in your persona -- it will be replaced with the `BOT_NAME` from `.env`.

## Project Structure

```
teams-voice-agent/
├── api_server.py              # FastAPI REST server
├── run.py                     # CLI entry point
├── start_api.bat              # Windows launcher
├── requirements.txt           # Python dependencies
├── .env.example               # Environment template
├── persona.txt.example        # Persona template
├── certs/                     # TLS certificates (self-signed)
├── docs/
│   └── ARCHITECTURE.md        # Detailed architecture docs
└── src/teams_agent/
    ├── __main__.py            # BotSession orchestrator
    ├── config.py              # Configuration loader
    ├── bridge.py              # Python-side WebSocket audio bridge
    ├── browser.py             # Playwright browser automation
    ├── pipeline.py            # Google Gemini S2S pipeline
    ├── pipeline_deepgram.py   # Deepgram Voice Agent pipeline
    ├── serializer.py          # PCM audio serializer
    ├── vision_observer.py     # Screenshot analysis via Gemini
    ├── meeting_memory.py      # ChromaDB meeting context store
    ├── transcript.py          # Conversation transcript collector
    └── js/
        └── audio_bridge.js    # Browser-side audio intercept
```

## How It Works

1. **Browser automation**: Playwright launches Chrome/Edge, navigates to the Teams meeting URL, and joins the meeting
2. **Audio interception**: `audio_bridge.js` is injected into the page, overriding `getUserMedia` and `RTCPeerConnection` to capture meeting audio and play bot audio
3. **WebSocket bridge**: Captured audio is sent from the browser to Python via `expose_function`, then forwarded over a TLS WebSocket to the Pipecat pipeline
4. **AI processing**: The pipeline sends audio to the cloud AI service (Gemini or Deepgram), which returns generated speech audio
5. **Playback**: Bot audio flows back through the WebSocket to the browser, where it's played into the meeting via the virtual microphone track
6. **Visual awareness** (optional): Periodic screenshots are analyzed by Gemini vision and the results are injected into the AI's system prompt
7. **Meeting memory**: Conversation transcripts are summarized and stored in ChromaDB for cross-meeting context

## Legal & Ethical Use

### Educational and Research Disclaimer

This project is developed strictly for **educational, research, and personal learning purposes**. Its primary goal is to explore the technical implementation of:

- Real-time Speech-to-Speech (S2S) architectures
- Multimodal RAG (Retrieval-Augmented Generation) systems
- Vision-led browser automation using Playwright and Gemini

This is a **Proof of Concept (PoC)** -- not a commercial product or service.

### Trademark & Non-Affiliation

- **Microsoft Teams** is a trademark of Microsoft Corporation. This project is **not affiliated with, sponsored by, or endorsed by Microsoft Corporation**. Any reference to Microsoft products is purely to describe technical compatibility and interoperability.
- **No proprietary assets**: No official Microsoft logos, icons, or proprietary code are included in this repository. All automation logic is original work using the Playwright browser automation framework.
- **No UI screenshots**: This repository does not include screenshots of the Microsoft Teams interface.

### Terms of Service & Compliance

- Users are responsible for ensuring their use complies with the [Microsoft Services Agreement](https://www.microsoft.com/en-us/servicesagreement) and the Teams Use Terms.
- **Browser automation**: This tool uses Playwright as a "Browser Bridge." Users should be aware that automated interaction with web interfaces may be subject to platform-specific restrictions. This project does not bypass, disable, or spoof any security controls.
- **Bot identification**: The agent **must** be clearly identified in meetings via its participant name (e.g., "AI Assistant", "Your Name's AI Agent"). Impersonating a human participant or hiding the bot's identity is a Terms of Service violation and is not supported by this project.
- **Official APIs**: For production use cases, consider using [Microsoft Graph API](https://learn.microsoft.com/en-us/graph/) and the [Teams Bot Framework](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/what-are-bots) instead of browser automation.

### Ethical AI & Privacy

- **Consent**: When using this agent in a live meeting, it is the user's ethical responsibility to **inform all participants** that an AI agent is present and processing audio/visual data.
- **Voice cloning**: The voice cloning functionality (via Cartesia) is intended **only for the user's own voice**. Cloning others' voices without their explicit consent is strictly prohibited and unethical.
- **Data handling**: This project does not store audio or image data by default. Meeting memory (ChromaDB) stores only text summaries. Users are responsible for the security and privacy of any data stored in their local database.
- **No data exfiltration**: This project does not transmit any data to third parties beyond the configured AI service providers (Google, Deepgram, OpenAI) as required for the voice pipeline.

### Copyright & IP Guidance

| Category | Risk | Mitigation |
|----------|------|------------|
| **Copyright** | Using proprietary logos, images, or code | No Microsoft-owned assets included |
| **Trademark** | Using names in a way that implies affiliation | Non-affiliation disclaimer at top of README |
| **Terms of Service** | Automating UI in ways that bypass security | Bot is clearly identified; no security bypass |
| **Privacy** | Processing meeting audio/video without consent | Consent disclosure required; no default data storage |

### License & Liability

This project is licensed under the **MIT License**. It is provided "as is," without warranty of any kind, express or implied. The author(s) shall not be held liable for any claims, damages, or other liability arising from the use of this software.

See [LICENSE](LICENSE) for the full license text.
