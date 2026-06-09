"""Allow `python -m bot` to launch the runner."""
import asyncio
from bot.runner import BotRunner

if __name__ == "__main__":
    asyncio.run(BotRunner().run_forever())
