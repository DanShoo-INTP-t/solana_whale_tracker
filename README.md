# 🐋 Solana Live Price & Whale Tracker

An asynchronous Telegram bot that tracks the live price of Solana (SOL) and
broadcasts "whale alerts" to every subscribed user.

> **Note:** The SOL price is live data from the CoinGecko API. The whale
> transactions are **simulated** (random numbers) for demonstration purposes.
> This project is for learning and is not financial advice.

## Screenshots

![Telegram chat](docs/telegram.png)
![Console logs](docs/console.png)
## Try it

Open the bot in Telegram: https://t.me/SOL_Whale_Tracker_xyz_bot

> The bot runs on my personal computer, so it may be offline.
> See the screenshots above for a demo.
## Features

- Live SOL/USD price from the free CoinGecko API (no API key needed)
- Simulated whale detection: any trade above 10,000 SOL triggers an alert
- Multi-user support: everyone who sends `/start` receives alerts
- Commands: `/start` (subscribe), `/stop` (unsubscribe), `/price` (current price)
- Informative, timestamped logging in the console
- Optional SOCKS5/HTTP proxy support for networks where Telegram is blocked

## Tech stack

Python 3.10+, [aiogram 3](https://docs.aiogram.dev/), httpx, python-dotenv, asyncio

## Getting started

1. **Clone the repository** and open the folder.
2. **Create a virtual environment and install dependencies:**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate        # on macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Create a bot:** open [@BotFather](https://t.me/BotFather) in Telegram,
   send `/newbot` and copy the token.
4. **Configure secrets:** copy `.env.example` to `.env` and paste your token.
   The `.env` file is listed in `.gitignore` and is never uploaded.
5. **Run the bot:**
   ```bash
   python main.py
   ```
6. Open your bot in Telegram and send `/start`.

## Design decisions

- **Fully asynchronous.** The bot uses `async`/`await` and `asyncio.sleep`, so a
  slow network call never blocks other users.
- **Secrets stay out of the code.** The bot token is loaded from a `.env` file.
- **Separated responsibilities.** Price fetching, whale simulation, message
  formatting and broadcasting are separate functions.
- **HTML parse mode instead of MarkdownV2.** MarkdownV2 requires escaping many
  characters, and one mistake makes Telegram reject the message.
- **Realistic simulation.** Trade sizes follow an exponential distribution, so
  most trades are small and whales are rare.
- **Resilient background worker.** Network errors are logged and the monitor
  keeps running; users who block the bot are removed automatically.

## Limitations and roadmap

- [ ] Store subscribers in a database (SQLite) so they survive restarts
      (currently an in-memory set)
- [ ] Replace the simulation with real Solana blockchain data (RPC)
- [ ] Add unit tests with `pytest`
- [ ] Add a Dockerfile for easy deployment