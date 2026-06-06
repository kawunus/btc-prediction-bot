import asyncio
import aiohttp
import logging

logger = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"

RETRIES = 3
RETRY_DELAY = 2  # seconds between retries


async def _fetch_binance(session: aiohttp.ClientSession) -> float:
    async with session.get(
        BINANCE_URL,
        params={"symbol": "BTCUSDT"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return float(data["price"])


async def _fetch_coingecko(session: aiohttp.ClientSession) -> float:
    async with session.get(
        COINGECKO_URL,
        params={"ids": "bitcoin", "vs_currencies": "usd"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return float(data["bitcoin"]["usd"])


async def get_btc_price() -> float:
    """
    Fetch current BTC/USDT price.
    Tries Binance up to RETRIES times, then falls back to CoinGecko (also with retries).
    Raises RuntimeError if all attempts fail.
    """
    async with aiohttp.ClientSession() as session:
        # Try Binance
        for attempt in range(1, RETRIES + 1):
            try:
                price = await _fetch_binance(session)
                logger.info(f"BTC price from Binance (attempt {attempt}): {price}")
                return price
            except Exception as e:
                logger.warning(f"Binance attempt {attempt}/{RETRIES} failed: {e}")
                if attempt < RETRIES:
                    await asyncio.sleep(RETRY_DELAY)

        # Fall back to CoinGecko
        logger.warning("All Binance attempts failed, switching to CoinGecko...")
        for attempt in range(1, RETRIES + 1):
            try:
                price = await _fetch_coingecko(session)
                logger.info(f"BTC price from CoinGecko (attempt {attempt}): {price}")
                return price
            except Exception as e:
                logger.warning(f"CoinGecko attempt {attempt}/{RETRIES} failed: {e}")
                if attempt < RETRIES:
                    await asyncio.sleep(RETRY_DELAY)

    raise RuntimeError("Failed to fetch BTC price from all sources after retries")
