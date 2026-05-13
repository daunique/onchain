"""
main.py
────────
OnChain Sentinel — Main Entry Point

Starts three concurrent services:
  1. Alchemy WebSocket monitor (new blocks → pattern engine)
  2. Telegram bot alerter
  3. Flask dashboard (REST API + web UI)

Usage:
  python main.py
"""

import os
import asyncio
import threading
from dotenv import load_dotenv

load_dotenv()

from engine.fingerprint   import FingerprintEngine
from engine.fetcher       import AlchemyMonitor
from bot.telegram_bot     import TelegramAlerter
from data.store           import store
from dashboard.app        import app


# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN",      "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID",        "")
ALCHEMY_WS_URL    = os.getenv("ALCHEMY_WS_URL",          "")
ALCHEMY_API_KEY   = os.getenv("ALCHEMY_API_KEY",         "")
ETHERSCAN_KEY     = os.getenv("ETHERSCAN_API_KEY",       "")
TOKEN_ADDRESS     = os.getenv("TOKEN_CONTRACT_ADDRESS",  "")
TOKEN_SYMBOL      = os.getenv("TOKEN_SYMBOL",            "TOKEN")
DASHBOARD_PORT    = int(os.getenv("DASHBOARD_PORT",      5000))

# ── Known manipulator wallets (add from your analysis) ────────────────────────
# These addresses were identified in your pump/dump investigation.
# The engine will flag them at high confidence regardless of gas settings.
KNOWN_WALLETS = [
    # "0xABC...123",  ← paste your identified wallets here
]

# ── Custom fingerprints ───────────────────────────────────────────────────────
FINGERPRINTS = [
    {
        "gas_limit":              200000,
        "max_priority_fee_gwei":  3,
        "max_fee_gwei":           6,
    },
    # Add more fingerprints as you discover them:
    # {
    #     "gas_limit":              150000,
    #     "max_priority_fee_gwei":  2,
    #     "max_fee_gwei":           4,
    # },
]


# ── Build components ──────────────────────────────────────────────────────────
engine  = FingerprintEngine(fingerprints=FINGERPRINTS, known_wallets=KNOWN_WALLETS)
alerter = TelegramAlerter(bot_token=TELEGRAM_TOKEN, chat_id=TELEGRAM_CHAT_ID)


def on_new_transaction(tx):
    """Called for every token transfer detected on-chain."""
    result = engine.analyze(tx)
    if result:
        # Save to dashboard store
        store.add(result.to_dict())
        # Send Telegram alert
        alerter.send_signal(result, token_symbol=TOKEN_SYMBOL)
        print(f"[Signal] {result.confidence.upper()} | score={result.score} | {result.pattern_label}")
    else:
        print(f"[Monitor] TX scanned — no match | from={tx.from_address[:10]}...")


# ── Daily summary scheduler ───────────────────────────────────────────────────
def start_daily_summary():
    import time
    from datetime import datetime, timedelta

    def loop():
        while True:
            now    = datetime.utcnow()
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            time.sleep(wait)
            stats = engine.get_stats()
            alerter.send_daily_summary(stats, TOKEN_SYMBOL)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


# ── Flask dashboard in background thread ─────────────────────────────────────
def start_dashboard():
    print(f"[Dashboard] Starting on port {DASHBOARD_PORT}...")
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)


# ── Main async runner ─────────────────────────────────────────────────────────
async def run_monitor():
    monitor = AlchemyMonitor(
        ws_url           = ALCHEMY_WS_URL,
        etherscan_key    = ETHERSCAN_KEY,
        contract_address = TOKEN_ADDRESS,
        on_transaction   = on_new_transaction,
    )
    await monitor.start()


def main():
    print("=" * 55)
    print("  ONCHAIN SENTINEL — GAS FINGERPRINT MONITOR")
    print("=" * 55)
    print(f"  Token:     ${TOKEN_SYMBOL} ({TOKEN_ADDRESS[:10]}...)")
    print(f"  Wallets:   {len(KNOWN_WALLETS)} known manipulators")
    print(f"  Patterns:  {len(FINGERPRINTS)} fingerprints loaded")
    print(f"  Dashboard: http://localhost:{DASHBOARD_PORT}")
    print("=" * 55)

    # Validate config
    missing = []
    if not TELEGRAM_TOKEN:   missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
    if not ALCHEMY_WS_URL:   missing.append("ALCHEMY_WS_URL")
    if not TOKEN_ADDRESS:    missing.append("TOKEN_CONTRACT_ADDRESS")
    if missing:
        print(f"\n⚠️  Missing env vars: {', '.join(missing)}")
        print("   Copy .env.example to .env and fill in your keys.\n")

    # Send startup message
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        alerter.send_startup_message(TOKEN_SYMBOL, TOKEN_ADDRESS)

    # Start dashboard in background thread
    dash_thread = threading.Thread(target=start_dashboard, daemon=True)
    dash_thread.start()

    # Start daily summary scheduler
    start_daily_summary()

    # Start monitor (async main loop)
    try:
        asyncio.run(run_monitor())
    except KeyboardInterrupt:
        print("\n[Sentinel] Shutdown requested. Goodbye.")


if __name__ == "__main__":
    main()
