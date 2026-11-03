"""Solana Live Price & Whale Tracker - an asynchronous Telegram bot.

What it does:
    * Users subscribe with /start and unsubscribe with /stop.
    * A background task polls the live SOL price (CoinGecko) every 10 seconds.
    * A simulated transaction is generated on every tick. If it exceeds the
      whale threshold, an alert is broadcast to every subscribed chat.

Design notes (useful talking points for an interview):
    * Fully asynchronous (aiogram 3 + httpx + asyncio), so one slow network
      call never freezes the bot for other users.
    * Secrets live in a .env file that is excluded from Git.
    * Price fetching, whale simulation, message formatting and broadcasting
      are separate functions, each with a single responsibility.

Requires Python 3.10+ (uses the `float | None` style of type hints).
"""

import asyncio
import contextlib
import html
import logging
import os
import random
from datetime import datetime, timezone
from typing import Literal

import httpx
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Constants are defined once, in UPPER_CASE, so behaviour can be tuned in one
# place instead of hunting for "magic numbers" scattered through the code.
COINGECKO_URL: str = "https://api.coingecko.com/api/v3/simple/price"
WHALE_THRESHOLD_SOL: int = 10_000        # Trades above this many SOL are "whales"
POLL_INTERVAL_SECONDS: int = 10          # Delay between market checks
HTTP_TIMEOUT_SECONDS: float = 10.0       # Never wait forever on a network call
AVERAGE_TRADE_SIZE_SOL: int = 3_000      # Mean of the simulated trade sizes
BROADCAST_DELAY_SECONDS: float = 0.05    # ~20 msgs/sec, below Telegram's ~30/sec limit
BASE58_ALPHABET: str = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
DIVIDER: str = "━" * 18

TradeDirection = Literal["BUY", "SELL"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Structured, timestamped logs make it easy to see what the bot is doing and
# to debug problems after the fact.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("sol_whale_bot")
# httpx logs every single request at INFO level. With a 10-second polling loop
# that would flood the console, so we only let it speak up for warnings.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
# Chat IDs of everyone who sent /start. A set gives O(1) add/remove and
# prevents duplicates automatically.
# Trade-off: it lives in memory, so it is wiped on restart. A production
# version would store it in a database such as SQLite or PostgreSQL.
active_users: set[int] = set()

router = Router()


class PriceFetchError(Exception):
    """Raised when the live SOL price cannot be retrieved or understood."""


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
def load_bot_token() -> str:
    """Load the Telegram bot token from the environment (.env file).

    The token is a password for the bot. Keeping it out of the source code
    means it can never leak through a public GitHub repository.

    Returns:
        The bot token string.

    Raises:
        RuntimeError: If BOT_TOKEN is missing. Failing fast with a clear
            message is better than crashing later with a confusing error.
    """
    load_dotenv()  # Reads key=value pairs from .env into os.environ
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is missing. Copy .env.example to .env and paste "
            "the token you got from @BotFather."
        )
    return token


# ---------------------------------------------------------------------------
# Data layer: live price
# ---------------------------------------------------------------------------
async def fetch_sol_price(client: httpx.AsyncClient) -> float:
    """Fetch the current SOL price in USD from the CoinGecko API.

    The HTTP client is passed in (dependency injection) instead of being
    created here. Reusing one client keeps connections open between calls,
    which is faster, and it makes this function easy to test with a fake client.

    Args:
        client: A shared asynchronous HTTP client.

    Returns:
        The price of one SOL in US dollars.

    Raises:
        PriceFetchError: On network problems, bad HTTP status codes
            (for example 429 "too many requests") or unexpected JSON.
    """
    try:
        response = await client.get(
            COINGECKO_URL,
            params={"ids": "solana", "vs_currencies": "usd"},
        )
        response.raise_for_status()  # Turn 4xx/5xx answers into exceptions
        return float(response.json()["solana"]["usd"])
    except httpx.HTTPError as exc:
        raise PriceFetchError(f"Network or HTTP error: {exc!r}") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise PriceFetchError(f"Unexpected API response format: {exc!r}") from exc


# ---------------------------------------------------------------------------
# Business logic: whale simulation
# ---------------------------------------------------------------------------
def simulate_transaction_volume() -> int:
    """Simulate the size of one on-chain transaction, in SOL.

    Real markets are "heavy-tailed": most trades are small and giant trades are
    rare. An exponential distribution models that well. A uniform random
    number would make whales far too common and would spam every user.

    Returns:
        A positive integer volume in SOL.
    """
    volume = int(random.expovariate(1 / AVERAGE_TRADE_SIZE_SOL))
    return max(1, volume)  # Never return 0 SOL


def is_whale_transaction(volume_sol: int) -> bool:
    """Decide whether a transaction is large enough to be called a whale move.

    Kept as its own tiny function so the rule is easy to find, change and test.

    Args:
        volume_sol: Transaction size in SOL.

    Returns:
        True if the volume is strictly above WHALE_THRESHOLD_SOL.
    """
    return volume_sol > WHALE_THRESHOLD_SOL


def generate_fake_wallet() -> str:
    """Create a random, shortened wallet address for the simulated trade.

    Returns:
        A string like "7xKX…9fQa" (first 4 and last 4 characters of a
        44-character base58-style address).
    """
    address = "".join(random.choices(BASE58_ALPHABET, k=44))
    return f"{address[:4]}…{address[-4:]}"


# ---------------------------------------------------------------------------
# Presentation layer: message formatting
# ---------------------------------------------------------------------------
def build_whale_alert(
    volume_sol: int,
    price_usd: float,
    direction: TradeDirection,
    wallet: str,
) -> str:
    """Build the HTML-formatted Telegram message for a whale alert.

    HTML parse mode is used instead of MarkdownV2 on purpose: MarkdownV2
    requires escaping characters such as '.', '-' and '!', and one missed
    escape makes Telegram reject the whole message. HTML is far less fragile.

    Args:
        volume_sol: Transaction size in SOL.
        price_usd: Current SOL price in USD.
        direction: Either "BUY" or "SELL".
        wallet: Shortened wallet address of the whale.

    Returns:
        The finished message text.
    """
    value_usd: float = volume_sol * price_usd
    direction_icon: str = "🟢" if direction == "BUY" else "🔴"
    timestamp: str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    return (
        "🐋🐋🐋 <b>WHALE ALERT</b> 🐋🐋🐋\n"
        f"{DIVIDER}\n"
        f"{direction_icon} <b>{direction}</b> detected on <b>Solana (SOL)</b>\n\n"
        f"💰 <b>Volume:</b> <code>{volume_sol:,} SOL</code>\n"
        f"💵 <b>Value:</b> <code>${value_usd:,.2f}</code>\n"
        f"📈 <b>Price:</b> <code>${price_usd:,.2f}</code>\n"
        f"👛 <b>Wallet:</b> <code>{wallet}</code>\n\n"
        f"🕒 {timestamp}\n"
        f"{DIVIDER}\n"
        "🧪 <i>Simulated transaction for demo purposes</i>"
    )


# ---------------------------------------------------------------------------
# Delivery: broadcasting to all subscribers
# ---------------------------------------------------------------------------
async def broadcast_message(bot: Bot, text: str) -> int:
    """Send a message to every subscribed chat.

    Args:
        bot: The aiogram Bot used to send messages.
        text: HTML-formatted message text.

    Returns:
        How many chats the message was delivered to.
    """
    delivered: int = 0

    # Iterate over a COPY of the set. We may remove users inside the loop, and
    # changing a set while looping over it raises a RuntimeError.
    for chat_id in list(active_users):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            delivered += 1
        except TelegramForbiddenError:
            # The user blocked the bot. Remove them so we stop retrying forever.
            active_users.discard(chat_id)
            logger.info("User %s blocked the bot - removed from subscribers.", chat_id)
        except TelegramAPIError as exc:
            # One failing chat must never stop alerts for everyone else.
            logger.warning("Could not deliver to chat %s: %s", chat_id, exc)

        # A short pause keeps us under Telegram's flood limits for large audiences.
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    return delivered


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
async def market_monitor(bot: Bot, client: httpx.AsyncClient) -> None:
    """Continuously watch the market and broadcast whale alerts.

    Runs as a background asyncio task next to the bot's command handling.
    Because it only uses `await` and `asyncio.sleep` (never time.sleep), the
    event loop stays free to answer users while this task is waiting.

    Args:
        bot: The aiogram Bot used for broadcasting.
        client: Shared HTTP client for price requests.
    """
    logger.info("Market monitor started (every %ss).", POLL_INTERVAL_SECONDS)

    while True:
        try:
            price_usd: float = await fetch_sol_price(client)
            volume_sol: int = simulate_transaction_volume()

            logger.info(
                "SOL = $%.2f | simulated trade = %s SOL | subscribers = %d",
                price_usd,
                f"{volume_sol:,}",
                len(active_users),
            )

            if is_whale_transaction(volume_sol):
                direction: TradeDirection = random.choice(["BUY", "SELL"])
                alert_text = build_whale_alert(
                    volume_sol=volume_sol,
                    price_usd=price_usd,
                    direction=direction,
                    wallet=generate_fake_wallet(),
                )
                delivered = await broadcast_message(bot, alert_text)
                logger.warning(
                    "WHALE ALERT: %s SOL %s - delivered to %d user(s).",
                    f"{volume_sol:,}",
                    direction,
                    delivered,
                )

        except PriceFetchError as exc:
            # Expected failure (offline, rate limit...): log it and try again.
            logger.warning("Skipping this tick: %s", exc)
        except Exception:
            # Unexpected bug: log the full traceback but keep the monitor alive.
            # (CancelledError is not caught here, so shutdown still works.)
            logger.exception("Unexpected error in market monitor.")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)  # Non-blocking pause


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Subscribe the chat to whale alerts and greet the user."""
    active_users.add(message.chat.id)
    logger.info("New subscriber: %s (total: %d)", message.chat.id, len(active_users))

    # Escape user-controlled text: a name like "<b>" must not break our HTML.
    name: str = html.escape(message.from_user.first_name) if message.from_user else "there"
    await message.answer(
        f"👋 Hello, <b>{name}</b>!\n\n"
        "🐋 You are now subscribed to <b>Solana Whale Alerts</b>.\n"
        f"I will notify you whenever a trade above "
        f"<code>{WHALE_THRESHOLD_SOL:,} SOL</code> is detected.\n\n"
        "📋 <b>Commands</b>\n"
        "/price - current SOL price\n"
        "/stop - unsubscribe from alerts"
    )


@router.message(Command("stop"))
async def handle_stop(message: Message) -> None:
    """Unsubscribe the chat. Giving users an opt-out is good privacy practice."""
    active_users.discard(message.chat.id)  # discard() will not fail if absent
    logger.info("Unsubscribed: %s (total: %d)", message.chat.id, len(active_users))
    await message.answer("🔕 You are unsubscribed. Send /start to subscribe again.")


@router.message(Command("price"))
async def handle_price(message: Message, http_client: httpx.AsyncClient) -> None:
    """Reply with the live SOL price.

    `http_client` is injected by aiogram from the dispatcher's workflow data
    (see main()), so handlers can share a single connection pool.
    """
    try:
        price_usd: float = await fetch_sol_price(http_client)
    except PriceFetchError as exc:
        logger.warning("/price failed: %s", exc)
        await message.answer("⚠️ Could not load the price right now. Please try again soon.")
        return

    await message.answer(f"💎 <b>Solana (SOL)</b>\n💵 <code>${price_usd:,.2f}</code> USD")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    """Wire everything together and run the bot until it is stopped."""
    load_dotenv()  # Сначала загружаем .env, чтобы были доступны PROXY_URL и BOT_TOKEN
    proxy_url = os.getenv("PROXY_URL")  # None, если в .env не указан
    session = AiohttpSession(proxy=proxy_url) if proxy_url else AiohttpSession()
    bot = Bot(
        token=load_bot_token(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    # `async with` guarantees the HTTP client is closed cleanly on shutdown.
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http_client:
        dispatcher["http_client"] = http_client  # Makes it available to handlers

        # Keep a reference to the task: asyncio only holds weak references, so
        # an unreferenced background task could be garbage-collected mid-run.
        monitor_task = asyncio.create_task(market_monitor(bot, http_client))

        try:
            logger.info("Bot is running. Press Ctrl+C to stop.")
            # Long polling: the bot asks Telegram for new messages. No public
            # server or webhook is needed, which is ideal for local development.
            await dispatcher.start_polling(bot)
        finally:
            # Stop the background worker before the program exits.
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            logger.info("Bot stopped.")


if __name__ == "__main__":
    # Ctrl+C raises KeyboardInterrupt; we catch it so the user sees a clean
    # exit instead of a long traceback.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())