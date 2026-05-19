"""
main.py  —  OnChain Sentinel v2
"""

import os
import sys
import asyncio
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
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
SCAN_INTERVAL_MIN    = int(os.getenv("SCAN_INTERVAL_MINUTES",  10))
DASHBOARD_PORT       = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "5000")))
MAX_ANALYZER_WORKERS = int(os.getenv("MAX_ANALYZER_WORKERS",   3))

alerter  = TelegramAlerter()
executor = ThreadPoolExecutor(max_workers=MAX_ANALYZER_WORKERS, thread_name_prefix="analyzer")

_analyzing      = set()
_analyzing_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts():
    return datetime.utcnow().strftime("%H:%M:%S")


def _safe_run(label: str, fn, *args, **kwargs):
    """Run fn(*args) catching and printing any exception with full traceback."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        print(f"\n[ERROR] {label} failed at {_ts()}:")
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        return None


# ── Signal handler ────────────────────────────────────────────────────────────

def on_staged_signal(signal: StagedSignal):
    d = signal.to_dict()
    store.add_signal(d)
    store.save()
    alerter.send_staged_signal(signal)
    print(f"[Signal] {signal.stage} conf={signal.confidence:.2f} "
          f"${signal.token_symbol} hub={signal.hub_address[:10]}...")
    sys.stdout.flush()


# ── Analyzer worker ───────────────────────────────────────────────────────────

def _analyze_worker(addr: str):
    try:
        result  = analyze_token(addr)
        wallets = result.get("wallets_added", 0) if result else 0
        if wallets > 0:
            for c in store.get_clusters(addr):
                alerter.send_new_cluster(c)
            alerter.send_scan_complete(1, wallets)
        print(f"[Analyzer] {addr[:10]}... done — {wallets} wallets added")
    except Exception:
        print(f"[Analyzer] ERROR for {addr[:10]}:")
        traceback.print_exc(file=sys.stdout)
    finally:
        with _analyzing_lock:
            _analyzing.discard(addr)
        sys.stdout.flush()


def submit_analysis(addr: str):
    with _analyzing_lock:
        if addr in _analyzing:
            return
        _analyzing.add(addr)
    executor.submit(_analyze_worker, addr)
    print(f"[Analyzer] Queued {addr[:10]}...")


# ── Scan cycle ────────────────────────────────────────────────────────────────

def run_scan_cycle():
    print(f"\n{'='*50}")
    print(f"  SCAN START  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*50}")
    sys.stdout.flush()

    flagged = _safe_run("scanner", run_scan)

    if flagged is None:
        print("[Scan] Cycle aborted due to error")
    elif not flagged:
        print("[Scan] Cycle complete — nothing flagged")
    else:
        print(f"[Scan] {len(flagged)} token(s) flagged — queuing analysis")
        for addr in flagged:
            submit_analysis(addr)

    print(f"{'='*50}\n")
    sys.stdout.flush()


# ── Catchup ───────────────────────────────────────────────────────────────────

def run_catchup():
    incomplete = [
        addr for addr, info in list(store.tokens.items())
        if info.get("flagged") and info.get("analysis_status") != "complete"
    ]
    if incomplete:
        print(f"[Catchup] {len(incomplete)} token(s) need analysis")
        for addr in incomplete:
            submit_analysis(addr)
    else:
        print("[Catchup] Nothing to catch up")
    sys.stdout.flush()


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron    import CronTrigger

    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)

    # First scan: 15 seconds after startup (give Flask time to bind)
    first_run = datetime.utcnow() + timedelta(seconds=15)

    scheduler.add_job(
        run_scan_cycle,
        IntervalTrigger(minutes=SCAN_INTERVAL_MIN, start_date=first_run),
        id="scan_cycle",
        next_run_time=first_run,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )

    # Catchup: 90 seconds after startup, then every 15 min
    scheduler.add_job(
        run_catchup,
        IntervalTrigger(minutes=15, start_date=datetime.utcnow() + timedelta(seconds=90)),
        id="catchup",
        next_run_time=datetime.utcnow() + timedelta(seconds=90),
        max_instances=1,
        coalesce=True,
    )

    # State save: every 5 min
    scheduler.add_job(
        store.save,
        IntervalTrigger(minutes=5),
        id="state_save",
    )

    # Daily summary: 09:00 UTC
    scheduler.add_job(
        lambda: alerter.send_daily_summary(store.stats()),
        CronTrigger(hour=9, minute=0),
        id="daily_summary",
    )

    scheduler.start()
    print(f"[Scheduler] Started — first scan in 15s, then every {SCAN_INTERVAL_MIN} min")
    print(f"[Scheduler] Catchup in 90s, then every 15 min")
    sys.stdout.flush()
    return scheduler


# ── WebSocket monitor ─────────────────────────────────────────────────────────

async def run_live_monitor():
    signal_engine = SignalEngine(store=store, on_staged_signal=on_staged_signal)

    def on_raw_tx(tx: dict):
        signal_engine.process(tx)

    monitor = LiveMonitor(store=store, on_signal=on_raw_tx)
    backoff = 5

    while True:
        try:
            print(f"[Monitor] Connecting... (backoff={backoff}s)")
            sys.stdout.flush()
            await monitor.start()
        except Exception as e:
            print(f"[Monitor] Disconnected: {e} — retry in {backoff}s")
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        else:
            backoff = 5


# ── Dashboard ─────────────────────────────────────────────────────────────────

def start_dashboard():
    print(f"[Dashboard] Starting on port {DASHBOARD_PORT}")
    sys.stdout.flush()
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║       ONCHAIN SENTINEL v2 — AUTONOMOUS MODE          ║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  Port:       {DASHBOARD_PORT}                                   ║")
    print(f"║  Scan every: {SCAN_INTERVAL_MIN} min  |  Workers: {MAX_ANALYZER_WORKERS}                   ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    missing = [v for v in [
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "ALCHEMY_WS_URL", "ALCHEMY_HTTP_URL", "ETHERSCAN_API_KEY",
    ] if not os.getenv(v)]
    if missing:
        print(f"WARNING: Missing env vars: {', '.join(missing)}")

    stats = store.stats()
    print(f"[State] tokens={stats['tokens_tracked']} clusters={stats['clusters_found']} "
          f"watchlist={stats['wallets_watched']} signals={stats['total_signals']}")
    print()
    sys.stdout.flush()

    alerter.send_startup(stats["wallets_watched"], stats["tokens_tracked"])

    threading.Thread(target=start_dashboard, daemon=True, name="dashboard").start()
    start_scheduler()

    try:
        asyncio.run(run_live_monitor())
    except KeyboardInterrupt:
        print("\n[Sentinel] Shutting down...")
        store.save()
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
