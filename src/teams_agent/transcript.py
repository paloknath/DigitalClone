"""Transcript collector for Deepgram Voice Agent conversation events."""

import asyncio
import time
from datetime import datetime


class TranscriptCollector:
    """Collects ConversationText events into a structured transcript."""

    def __init__(self):
        self._entries: list[dict] = []
        self._lock = asyncio.Lock()

    async def add_entry(self, role: str, content: str) -> None:
        async with self._lock:
            self._entries.append({
                "role": role,
                "content": content,
                "timestamp": time.time(),
            })

    def get_formatted_transcript(self) -> str:
        lines = []
        for entry in self._entries:
            ts = datetime.fromtimestamp(entry["timestamp"]).strftime("%H:%M:%S")
            lines.append(f"[{ts}] {entry['role'].upper()}: {entry['content']}")
        return "\n".join(lines)

    @property
    def entry_count(self) -> int:
        return len(self._entries)
