"""
analyzer/analyzer.py
─────────────────────
Orchestrates the full analysis pipeline for a flagged token:
  1. Pull pump/dump windows from store
  2. Download transfer history around those windows
  3. Enrich with gas data
  4. Run clustering
  5. Add suspicious wallets to watchlist
  6. Store discovered fingerprints for live monitoring
"""

import os
from datetime import datetime
from data.store import store
from analyzer.history import fetch_transfers_for_window, enrich_with_gas
from analyzer.clustering import cluster_wallets

ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "")


def analyze_token(token_address: str) -> dict:
    """
    Full analysis of one token.
    Returns summary dict with cluster count and wallets added to watchlist.
    """
    token_info = store.get_token(token_address)
    symbol     = token_info.get("symbol", "?")
    print(f"\n[Analyzer] ══ Analyzing {symbol} ({token_address[:12]}...) ══")

    # Get pump/dump windows for this token
    windows = store.get_pump_events(token_address)
    if not windows:
        print(f"[Analyzer] No pump windows for {symbol}, skipping")
        return {"status": "skipped", "reason": "no_pump_windows"}

    # Download transfers around all windows (merge into one big pull)
    all_transfers = []
    seen_hashes   = set()

    for window in windows:
        transfers = fetch_transfers_for_window(
            token_address  = token_address,
            window_start   = window["window_start"],
            window_end     = window["window_end"],
            api_key        = ETHERSCAN_KEY,
            padding_hours  = 24,
        )
        for tx in transfers:
            h = tx.get("hash", "")
            if h and h not in seen_hashes:
                seen_hashes.add(h)
                all_transfers.append(tx)

    if not all_transfers:
        print(f"[Analyzer] No transfers found for {symbol}")
        store.upsert_token(token_address, {"last_analyzed": datetime.utcnow().isoformat(), "analysis_status": "no_transfers"})
        return {"status": "done", "transfers": 0, "clusters": 0, "wallets_added": 0}

    # Enrich with gas data
    enriched = enrich_with_gas(all_transfers, ETHERSCAN_KEY)

    # Run clustering
    clusters = cluster_wallets(enriched, token_address, symbol)

    # Add clusters to store and build watchlist
    wallets_added = 0
    fingerprints_added = []

    for cluster in clusters:
        store.add_cluster(cluster.to_dict())

        for wallet in cluster.wallets:
            store.add_to_watchlist(wallet, {
                "symbol":      symbol,
                "token":       token_address.lower(),
                "cluster_score": cluster.score,
                "cluster_label": cluster.label,
                "fingerprint": cluster.fingerprint.to_dict(),
                "reason":      f"Gas fingerprint cluster — {cluster.label}",
            })
            wallets_added += 1

        # Register fingerprint for live monitoring
        fp_key = (
            cluster.fingerprint.gas_limit,
            cluster.fingerprint.max_priority_fee,
            cluster.fingerprint.max_fee,
        )
        if fp_key not in fingerprints_added:
            fingerprints_added.append(fp_key)

    # Update token record
    store.upsert_token(token_address, {
        "last_analyzed":     datetime.utcnow().isoformat(),
        "analysis_status":   "complete",
        "transfers_analyzed": len(enriched),
        "clusters_found":    len(clusters),
        "wallets_added":     wallets_added,
    })

    store.save()

    summary = {
        "status":        "complete",
        "symbol":        symbol,
        "token":         token_address,
        "transfers":     len(enriched),
        "clusters":      len(clusters),
        "wallets_added": wallets_added,
        "fingerprints":  fingerprints_added,
    }
    print(f"[Analyzer] ✅ Done: {len(clusters)} clusters, {wallets_added} wallets added to watchlist")
    return summary


def analyze_all_flagged() -> list[dict]:
    """Run analysis on all tokens currently flagged by the scanner."""
    results = []
    for addr, info in store.tokens.items():
        if info.get("flagged") and info.get("analysis_status") != "complete":
            result = analyze_token(addr)
            results.append(result)
    return results
