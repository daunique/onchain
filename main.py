"""
main.py
────────
OnChain Sentinel v2 — Fully Autonomous Entry Point

Services started:
  1. APScheduler  — runs token scan every 30 min
  2. APScheduler  — runs analyzer on flagged tokens after each scan
  3. APScheduler  — daily Telegram summary at 09:00 UTC
  4. asyncio      — Alchemy WebSocket live monitor (permanent loop)
  5. Flask thread — dashboard + REST API on port 5000

Zero manual input needed. The system discovers, analyzes, and monitors
completely on its own.
"""

import os
import asyncio
import threading
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from data.store            import store
from scanner.token_scanner import run_scan
from analyzer.analyzer     import analyze_token, analyze_all_flagged
from monitor.live_monitor  import LiveMonitor
from monitor.signal_engine import SignalEngine, StagedSignal
from bot.telegram_bot      import TelegramAlerter
from dashboard.app         import app


# ── Config ────────────────────────────────────────────────────────────────────
SCAN_INTERVAL_MIN = int(os.getenv("SCAN_INTERVAL_MINUTES", 30))
DASHBOARD_PORT    = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "5000")))

alerter = TelegramAlerter()


# ── Signal handler ────────────────────────────────────────────────────────────

def on_staged_signal(signal: StagedSignal):
    """Called by SignalEngine when a staged signal fires."""
    d = signal.to_dict()
    store.add_signal(d)
    store.save()
    alerter.send_staged_signal(signal)
    print(f"[Signal] {signal.stage} | conf={signal.confidence:.2f} | ${signal.token_symbol} | "
          f"hub={signal.hub_address[:10]}...")


# ── Scan + analyze cycle ──────────────────────────────────────────────────────

def run_full_cycle():
    """Full discovery cycle: scan → filter → analyze → build watchlist."""
    print(f"\n{'='*55}")
    print(f"  CYCLE START {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*55}")

    # 1. Scan for suspicious CEX-listed tokens
    flagged = run_scan()

    # 2. Analyze each flagged token (download history + cluster)
    wallets_added = 0
    for addr in flagged:
        try:
            result = analyze_token(addr)
            w = result.get("wallets_added", 0)
            wallets_added += w
            if w > 0:
                # Notify about new clusters found
                clusters = store.get_clusters(addr)
                for c in clusters:
                    alerter.send_new_cluster(c)
        except Exception as e:
            print(f"[Main] Analyze error for {addr[:10]}: {e}")
        time.sleep(1)

    # 3. Notify scan complete
    if flagged:
        alerter.send_scan_complete(len(flagged), wallets_added)

    print(f"{'='*55}")
    print(f"  CYCLE DONE  — {len(flagged)} tokens, {wallets_added} wallets added")
    print(f"{'='*55}\n")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone="UTC")

    # Full discovery cycle every N minutes
    scheduler.add_job(
        run_full_cycle,
        IntervalTrigger(minutes=SCAN_INTERVAL_MIN),
        id="full_cycle",
        next_run_time=datetime.utcnow(),  # run immediately on startup
    )

    # Daily summary at 09:00 UTC
    scheduler.add_job(
        lambda: alerter.send_daily_summary(store.stats()),
        CronTrigger(hour=9, minute=0),
        id="daily_summary",
    )

    scheduler.start()
    print(f"[Scheduler] Started — scan every {SCAN_INTERVAL_MIN} min")
    return scheduler


# ── Live monitor (async) ──────────────────────────────────────────────────────

async def run_live_monitor():
    """Async WebSocket monitor — runs forever in the main thread."""
    signal_engine = SignalEngine(store=store, on_staged_signal=on_staged_signal)

    def on_raw_tx(tx: dict):
        signal_engine.process(tx)

    monitor = LiveMonitor(store=store, on_signal=on_raw_tx)

    # Retry loop — reconnect on disconnection
    while True:
        try:
            await monitor.start()
        except Exception as e:
            print(f"[Monitor] Disconnected: {e} — reconnecting in 10s...")
            await asyncio.sleep(10)


# ── Dashboard thread ──────────────────────────────────────────────────────────

def start_dashboard():
    print(f"[Dashboard] Starting on port {DASHBOARD_PORT}")
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║       ONCHAIN SENTINEL v2 — AUTONOMOUS MODE          ║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  Dashboard:  http://localhost:{DASHBOARD_PORT}                  ║")
    print(f"║  Scan cycle: every {SCAN_INTERVAL_MIN} minutes                       ║")
    print("║  Pipeline:   Scan→Filter(CEX)→History→Cluster→Watch  ║")
    print("║  Alerts:     HUB_BROADCAST→FANOUT→CEX_DEPOSIT         ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    # Validate env
    missing = [v for v in [
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "ALCHEMY_WS_URL", "ALCHEMY_HTTP_URL", "ETHERSCAN_API_KEY"
    ] if not os.getenv(v)]
    if missing:
        print(f"⚠️  Missing env vars: {', '.join(missing)}")
        print("   Copy .env.example → .env and fill in your keys\n")

    # Print current state
    stats = store.stats()
    print(f"[State] Tokens: {stats['tokens_tracked']} | "
          f"Clusters: {stats['clusters_found']} | "
          f"Watchlist: {stats['wallets_watched']} | "
          f"Signals: {stats['total_signals']}")
    print()

    # Send startup message
    alerter.send_startup(stats["wallets_watched"], stats["tokens_tracked"])

    # Start dashboard in background thread
    threading.Thread(target=start_dashboard, daemon=True).start()

    # Start scheduler (scan cycles) in background thread
    start_scheduler()

    # Run live monitor in async main loop (blocks forever)
    try:
        asyncio.run(run_live_monitor())
    except KeyboardInterrupt:
        print("\n[Sentinel] Shutdown requested. State saved.")
        store.save()


if __name__ == "__main__":
    main()
