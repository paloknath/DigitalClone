"""Meeting memory: ChromaDB storage + Gemini summary generation."""

import asyncio
import logging
import time
from datetime import datetime

import chromadb
from google import genai

logger = logging.getLogger("teams_agent.meeting_memory")


class MeetingMemory:
    """Manages meeting summaries and cross-meeting context via ChromaDB."""

    def __init__(self, db_path: str, google_api_key: str, gemini_model: str):
        self._genai_client = genai.Client(api_key=google_api_key)
        self._gemini_model = gemini_model
        self._chroma_client = chromadb.PersistentClient(path=db_path)
        self._collection = self._chroma_client.get_or_create_collection(
            name="meeting_summaries",
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("MeetingMemory initialized (db=%s, model=%s)", db_path, gemini_model)

    async def generate_summary(self, transcript_text: str, meeting_url: str) -> str:
        prompt = (
            "You are a meeting summarizer. Given the following meeting transcript, "
            "produce a concise summary covering:\n"
            "1. Key topics discussed\n"
            "2. Decisions made\n"
            "3. Action items\n"
            "4. Participants and their main contributions\n\n"
            f"Meeting URL: {meeting_url}\n\n"
            f"TRANSCRIPT:\n{transcript_text}\n\n"
            "SUMMARY:"
        )
        response = await self._genai_client.aio.models.generate_content(
            model=self._gemini_model,
            contents=prompt,
        )
        return response.text or ""

    async def store_meeting(
        self,
        meeting_url: str,
        summary: str,
        transcript_text: str,
        max_stored: int = 20,
    ) -> str:
        meeting_id = f"meeting_{int(time.time())}_{abs(hash(meeting_url)) % 10000}"
        metadata = {
            "meeting_url": meeting_url,
            "timestamp": time.time(),
            "date": datetime.now().isoformat(),
            "transcript": transcript_text[:5000],
        }
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._collection.add(
                documents=[summary],
                metadatas=[metadata],
                ids=[meeting_id],
            ),
        )

        # Evict oldest meetings beyond max_stored
        await self._evict_old_meetings(max_stored)

        return meeting_id

    async def _evict_old_meetings(self, max_stored: int) -> None:
        loop = asyncio.get_event_loop()
        all_data = await loop.run_in_executor(
            None,
            lambda: self._collection.get(include=["metadatas"]),
        )
        ids = all_data["ids"]
        if len(ids) <= max_stored:
            return

        # Sort by timestamp ascending, delete oldest
        paired = list(zip(ids, all_data["metadatas"]))
        paired.sort(key=lambda x: x[1].get("timestamp", 0))
        to_delete = [p[0] for p in paired[: len(ids) - max_stored]]
        await loop.run_in_executor(
            None,
            lambda: self._collection.delete(ids=to_delete),
        )
        logger.info("Evicted %d old meetings (kept %d)", len(to_delete), max_stored)

    async def retrieve_context(
        self,
        query: str,
        meeting_url: str | None = None,
        n_results: int = 3,
    ) -> str:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            lambda: self._collection.query(
                query_texts=[query],
                n_results=n_results,
            ),
        )
        if not results or not results["documents"] or not results["documents"][0]:
            return ""

        parts = []
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            date = meta.get("date", "unknown")
            parts.append(f"--- Past Meeting ({date}) ---\n{doc}")
        return "\n\n".join(parts)
