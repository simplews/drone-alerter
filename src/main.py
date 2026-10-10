import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    logger.info("IF Drone Alert MVP starting...")
    # TODO: Initialize DB, setup Telegram MTProto listener, setup aiogram bot, and FastAPI status server
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
