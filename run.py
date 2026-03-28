"""Entry point for the Teams Audio Agent."""

from src.teams_agent.__main__ import main
import asyncio

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
