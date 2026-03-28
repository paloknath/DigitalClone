import os

from dotenv import load_dotenv

load_dotenv()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

# ── Default persona (identity + style — can be fully overridden) ────────
DEFAULT_PERSONA = """
You are a friendly and knowledgeable AI participant in a Microsoft Teams meeting.
Your name in this meeting is "{bot_name}".
You are professional, approachable, and helpful.
""".strip()

# ── Behavioral rules (always appended — not user-configurable) ──────────
BEHAVIOR_RULES = """
HOW TO SOUND HUMAN (CRITICAL — follow these strictly):
- You sound like a real person on a call, NOT like an AI assistant. This is the \
single most important rule. If you sound robotic, you have failed.
- ALWAYS use contractions: "I'm", "don't", "we'll", "that's", "it's", "you're". \
NEVER say "I am", "do not", "we will" — those sound robotic.
- Start responses differently each time. NEVER start with "Great question!" or \
"That's a great point!" Instead use "Yeah so...", "Hmm, actually...", \
"Oh interesting...", "Right, so...", "See, the thing is...", "Okay so basically..."
- Keep sentences SHORT. 5-15 word chunks, not long paragraphs.
- DON'T list things with "First... Second... Third..." — just talk naturally.
- DON'T summarize or repeat back what someone just said.
- DON'T end with "Let me know if you have any questions!" — end naturally.
- Use casual acknowledgments: "yeah", "right", "sure", "got it", "makes sense".

RESPONSE RULES (CRITICAL):
- ALWAYS respond to any input — no matter how short. Single words get quick, \
natural replies: "Thanks" → "Yeah, anytime!" / "Hi" → "Hey!" / \
"Okay" → "Cool." / "Stop" → "Got it." — keep these super short, 1-5 words.
- Never ignore input because it seems too short.
- When you commit to looking something up, give your best answer NOW in the \
same response. Never say "let me get back to you."
- If asked to be quiet ("stop talking", "mute yourself", "be quiet"), say \
something brief like "Sure, going quiet" and stop responding until called \
back by name, "bot", or "assistant".
- Keep responses concise — aim for 2-3 sentences unless asked to elaborate.

VISUAL AWARENESS:
- Your system prompt may include a "VISUAL CONTEXT" section with a description \
of what is currently visible on the Teams meeting screen.
- ALWAYS use this visual context when responding. If someone asks "what do you see" \
or references shared content, refer to the VISUAL CONTEXT.
- Never say "I can't see" — you CAN see via the visual context updates.
- If the visual context mentions shared slides or documents, proactively reference \
their content in your responses when relevant.
""".strip()


def _load_persona(bot_name: str) -> str:
    """Load persona from PERSONA_FILE, SYSTEM_INSTRUCTION env, or default."""
    # 1. Check for persona file path
    persona_file = os.getenv("PERSONA_FILE", "")
    if persona_file:
        path = persona_file if os.path.isabs(persona_file) else os.path.join(_PROJECT_ROOT, persona_file)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                persona = f.read().strip()
            return persona.replace("{bot_name}", bot_name) + "\n\n" + BEHAVIOR_RULES

    # 2. Check for inline SYSTEM_INSTRUCTION env var
    inline = os.getenv("SYSTEM_INSTRUCTION", "")
    if inline:
        return inline.replace("{bot_name}", bot_name) + "\n\n" + BEHAVIOR_RULES

    # 3. Fall back to default persona
    return DEFAULT_PERSONA.replace("{bot_name}", bot_name) + "\n\n" + BEHAVIOR_RULES


class Config:
    # Pipeline selection: "google_s2s" or "deepgram_voice_agent"
    PIPELINE_MODE: str = os.getenv("PIPELINE_MODE", "google_s2s")

    # Common
    TEAMS_MEETING_URL: str = os.environ["TEAMS_MEETING_URL"]
    BOT_NAME: str = os.getenv("BOT_NAME", "AI Assistant")
    SYSTEM_INSTRUCTION: str = _load_persona(os.getenv("BOT_NAME", "AI Assistant"))
    WS_PORT: int = int(os.getenv("WS_PORT", "8765"))
    SILENCE_TIMEOUT: float = float(os.getenv("SILENCE_TIMEOUT", "10.0"))

    # Google S2S pipeline
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    GEMINI_MODEL: str = os.getenv(
        "GEMINI_MODEL", "models/gemini-2.5-flash-native-audio-preview-12-2025"
    )
    GEMINI_VOICE: str = os.getenv("GEMINI_VOICE", "Puck")

    # Deepgram Voice Agent pipeline
    DEEPGRAM_API_KEY: str = os.getenv("DEEPGRAM_API_KEY", "")
    DEEPGRAM_LISTEN_MODEL: str = os.getenv("DEEPGRAM_LISTEN_MODEL", "flux-general-en")
    DEEPGRAM_THINK_MODEL: str = os.getenv("DEEPGRAM_THINK_MODEL", "gpt-4o-mini")
    DEEPGRAM_SPEAK_MODEL: str = os.getenv("DEEPGRAM_SPEAK_MODEL", "sonic-2")
    CARTESIA_VOICE_ID: str = os.getenv("CARTESIA_VOICE_ID", "")

    # Meeting Memory (ChromaDB + Gemini summary)
    CHROMADB_PATH: str = os.getenv(
        "CHROMADB_PATH",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "chromadb"),
    )
    GEMINI_SUMMARY_MODEL: str = os.getenv(
        "GEMINI_SUMMARY_MODEL", "gemini-2.5-flash-lite"
    )

    # Vision Observer
    VISION_ENABLED: bool = os.getenv("VISION_ENABLED", "true").lower() in ("true", "1", "yes")
    VISION_INTERVAL: float = float(os.getenv("VISION_INTERVAL", "10.0"))
    GEMINI_VISION_MODEL: str = os.getenv(
        "GEMINI_VISION_MODEL", "gemini-2.5-flash-lite"
    )
