"""
main.py
────────
OnChain Sentinel v2 — Fully Autonomous Entry Point

Architecture:
  ┌─────────────────────────────────────────────────────┐
  │  Main thread  → asyncio event loop (WebSocket)      │
  │  Thread 1     → Flask dashboard                     │
  │  Thread 2     → APScheduler (scan every 10 min)     │
  │  Thread pool  → Analyzer workers (non-blocking)     │
  └─────────────────────────────────────────────────────┘

Key improvements over v1:
  - Scan interval: 10 min (was 30)
  - Analyzer runs in ThreadPoolExecutor — never blocks the scanner
  - Scan and analyze are fully decoupled: scanner queues work,
    workers pick it up immediately in parallel
  - Re-analysis cooldown: 6h per token (unchanged)
  - WebSocket auto-reconnects with exponential backoff
  - State saved after every signal and every scan
"""

import os
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from data.store            import store
from scanner.token_scanner import run_scan
from analyzer.analyzer     import analyze_token
from monitor.live_monitor  import LiveMonitor
from monitor.signal_engine import SignalEngine, StagedSignal
from bot.telegram_bot      import TelegramAlerter
from dashboard.app         import app


# ── Config ────────────────────────────────────────────────────────────────────
SCAN_INTERVAL_MIN  = int(os.getenv("SCAN_INTERVAL_MINUTES", 10))   # 10 min default
DASHBOARD_PORT     = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "5000")))
MAX_ANALYZER_WORKERS = int(os.getenv("MAX_ANALYZER_WORKERS", 3))   # parallel analysis

alerter  = TelegramAlerter()
executor = ThreadPoolExecutor(max_workers=MAX_ANALYZER_WORKERS, thread_name_prefix="analyzer")

# Track which tokens are currently being analyzed to avoid duplicates
_analyzing: set[str] = set()
_analyzing_lock = threading.Lock()


# ── Signal handler ────────────────────────────────────────────────────────────

def on_staged_signal(signal: StagedSignal):
    """Called by SignalEngine every time a staged signal fires."""
    d = signal.to_dict()
    store.add_signal(d)
    store.save()
    alerter.send_staged_signal(signal)
    print(
        f"[Signal] {signal.stage} | conf={signal.confidence:.2f} "
        f"| ${signal.token_symbol} | hub={signal.hub_address[:10]}..."
    )


# ── Analyzer worker (runs in thread pool) ─────────────────────────────────────

def _analyze_worker(addr: str):
    """
    Runs in a background thread. Downloads history, clusters wallets,
    adds to watchlist. Completely non-blocking to the scanner.
    """
    try:
        result = analyze_token(addr)
        wallets = result.get("wallets_added", 0)

        if wallets > 0:
            clusters = store.get_clusters(addr)
            for c in clusters:
                alerter.send_new_cluster(c)
            alerter.send_scan_complete(1, wallets)
            print(f"[Analyzer] {addr[:10]}... → {wallets} wallets added to watchlist")
        else:
            print(f"[Analyzer] {addr[:10]}... → no new wallets")

    except Exception as e:
        print(f"[Analyzer] Error for {addr[:10]}: {e}")
    finally:
        with _analyzing_lock:
            _analyzing.discard(addr)


def submit_analysis(addr: str):
    """Submit a token for async analysis if not already in progress."""
    with _analyzing_lock:
        if addr in _analyzing:
            print(f"[Analyzer] {addr[:10]}... already in queue, skipping")
            return
        _analyzing.add(addr)
    executor.submit(_analyze_worker, addr)
    print(f"[Analyzer] Queued {addr[:10]}... for background analysis")


# ── Scan cycle (runs every 10 min) ────────────────────────────────────────────

def run_scan_cycle():
    """
    Scan only — fast. Hands flagged tokens to the thread pool immediately
    and returns. The scanner is never blocked waiting for analysis.
    """
    print(f"\n{'='*55}")
    print(f"  SCAN  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*55}")

    try:
        flagged = run_scan()
    except Exception as e:
        print(f"[Scan] Error: {e}")
        return

    if not flagged:
        print("[Scan] Nothing flagged this cycle")
        return

    print(f"[Scan] {len(flagged)} token(s) flagged → submitting to analyzer pool")
    for addr in flagged:
        submit_analysis(addr)

    print(f"{'='*55}\n")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)

    # Scan every 10 minutes — runs immediately on startup
    scheduler.add_job(
        run_scan_cycle,
        IntervalTrigger(minutes=SCAN_INTERVAL_MIN),
        id="scan_cycle",
        next_run_time=datetime.utcnow(),
        max_instances=1,        # never overlap scan cycles
        coalesce=True,          # skip missed runs, don't pile up
    )

    # Re-analyze any previously flagged tokens that weren't completed
    # Runs 2 minutes after startup to let the first scan settle
    def catchup_analysis():
        print("[Analyzer] Running catchup analysis on previously flagged tokens...")
        for addr, info in list(store.tokens.items()):
            if info.get("flagged") and info.get("analysis_status") != "complete":
                submit_analysis(addr)

    scheduler.add_job(
        catchup_analysis,
        IntervalTrigger(minutes=2),
        id="catchup",
        max_instances=1,
    )

    # Daily summary at 09:00 UTC
    scheduler.add_job(
        lambda: alerter.send_daily_summary(store.stats()),
        CronTrigger(hour=9, minute=0),
        id="daily_summary",
    )

    # Periodic state save every 5 minutes (safety net)
    scheduler.add_job(
        store.save,
        IntervalTrigger(minutes=5),
        id="state_save",
    )

    scheduler.start()
    print(f"[Scheduler] Started — scan every {SCAN_INTERVAL_MIN} min | {MAX_ANALYZER_WORKERS} analyzer workers")
    return scheduler


# ── Live WebSocket monitor ────────────────────────────────────────────────────

async def run_live_monitor():
    """WebSocket monitor — permanent async loop with exponential backoff."""
    signal_engine = SignalEngine(store=store, on_staged_signal=on_staged_signal)

    def on_raw_tx(tx: dict):
        signal_engine.process(tx)

    monitor  = LiveMonitor(store=store, on_signal=on_raw_tx)
    backoff  = 5   # seconds, doubles on each failure up to 60s

    while True:
        try:
            print("[Monitor] Connecting to Alchemy WebSocket...")
            await monitor.start()
        except Exception as e:
            print(f"[Monitor] Disconnected: {e} — reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)   # cap at 60s
        else:
            backoff = 5   # reset on clean disconnect


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
    print(f"║  Scan cycle: every {SCAN_INTERVAL_MIN} min                          ║")
    print(f"║  Analyzer:   {MAX_ANALYZER_WORKERS} parallel workers (non-blocking)     ║")
    print("║  Pipeline:   Scan → Analyze (async) → Watch live     ║")
    print("║  Alerts:     HUB_BROADCAST→FANOUT→CEX_DEPOSIT         ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    # Validate env vars
    missing = [v for v in [
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "ALCHEMY_WS_URL", "ALCHEMY_HTTP_URL", "ETHERSCAN_API_KEY",
    ] if not os.getenv(v)]
    if missing:
        print(f"⚠️  Missing env vars: {', '.join(missing)}")
        print("   Copy .env.example → .env and fill in your keys\n")

    # Print restored state
    stats = store.stats()
    print(
        f"[State] Tokens: {stats['tokens_tracked']} | "
        f"Clusters: {stats['clusters_found']} | "
        f"Watchlist: {stats['wallets_watched']} | "
        f"Signals: {stats['total_signals']}"
    )
    print()

    # Send startup Telegram message
    alerter.send_startup(stats["wallets_watched"], stats["tokens_tracked"])

    # Start Flask dashboard in background thread
    threading.Thread(target=start_dashboard, daemon=True, name="dashboard").start()

    # Start scheduler (scan + save jobs) in background thread
    start_scheduler()

    # Run WebSocket monitor on main asyncio loop (blocks forever)
    try:
        asyncio.run(run_live_monitor())
    except KeyboardInterrupt:
        print("\n[Sentinel] Shutdown requested. Saving state...")
        store.save()
        executor.shutdown(wait=False)
        print("[Sentinel] Done.")


if __name__ == "__main__":
    main()
