"""
analyzer/analyzer.py  (v2 — ETH + Base)
─────────────────────────────────────────
Orchestrates the full analysis pipeline for a flagged token.
Chain-aware: routes Etherscan calls via the token's chain field.
"""

import os
from datetime import datetime
from data.store import store
from analyzer.history import fetch_transfers_for_window, enrich_with_gas
from analyzer.clustering import cluster_wallets

ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "")


def analyze_token(token_address: str) -> dict:
    token_info = store.get_token(token_address)
    symbol     = token_info.get("symbol", "?")
    chain      = token_info.get("chain", "ethereum")   # default ETH for old store entries

    print(f"\n[Analyzer] ══ {symbol} [{chain}] ({token_address[:12]}...) ══")

    last_analyzed = token_info.get("last_analyzed")
    if last_analyzed:
        try:
            last = datetime.fromisoformat(last_analyzed)
            age  = (datetime.utcnow() - last).total_seconds()
            if age < 21600:
                print(f"[Analyzer] {symbol} analyzed {int(age/60)}min ago — skipping")
                return {"status": "skipped", "reason": "cooldown"}
        except Exception:
            pass

    windows = store.get_pump_events(token_address)
    if not windows:
        print(f"[Analyzer] No pump windows for {symbol}")
        return {"status": "skipped", "reason": "no_pump_windows"}

    print(f"[Analyzer] {symbol} — {len(windows)} pump window(s) to analyze")

    all_transfers = []
    seen_hashes   = set()

    for i, window in enumerate(windows):
        print(f"[Analyzer] Fetching window {i+1}/{len(windows)}...")
        transfers = fetch_transfers_for_window(
            token_address = token_address,
            window_start  = window["window_start"],
            window_end    = window["window_end"],
            api_key       = ETHERSCAN_KEY,   # kept for compat; routing is chain-based
            padding_hours = 24,
            chain         = chain,
        )
        for tx in transfers:
            h = tx.get("hash", "")
            if h and h not in seen_hashes:
                seen_hashes.add(h)
                all_transfers.append(tx)

    if not all_transfers:
        print(f"[Analyzer] No transfers found for {symbol}")
        store.upsert_token(token_address, {
            "last_analyzed":   datetime.utcnow().isoformat(),
            "analysis_status": "no_transfers",
        })
        return {"status": "done", "transfers": 0, "clusters": 0, "wallets_added": 0}

    print(f"[Analyzer] {symbol} — {len(all_transfers)} unique transfers, enriching...")

    enriched = enrich_with_gas(all_transfers, ETHERSCAN_KEY, chain=chain)
    clusters = cluster_wallets(enriched, token_address, symbol)

    wallets_added = 0
    for cluster in clusters:
        store.add_cluster(cluster.to_dict())
        for wallet in cluster.wallets:
            store.add_to_watchlist(wallet, {
                "symbol":        symbol,
                "token":         token_address.lower(),
                "chain":         chain,
                "cluster_score": cluster.score,
                "cluster_label": cluster.label,
                "fingerprint":   cluster.fingerprint.to_dict(),
                "reason":        f"Gas fingerprint cluster [{chain}] — {cluster.label}",
            })
            wallets_added += 1

    store.upsert_token(token_address, {
        "last_analyzed":      datetime.utcnow().isoformat(),
        "analysis_status":    "complete",
        "transfers_analyzed": len(enriched),
        "clusters_found":     len(clusters),
        "wallets_added":      wallets_added,
    })
    store.save()

    print(f"[Analyzer] ✅ {symbol} [{chain}] — {len(clusters)} clusters, "
          f"{wallets_added} wallets → watchlist")
    return {
        "status":        "complete",
        "symbol":        symbol,
        "token":         token_address,
        "chain":         chain,
        "transfers":     len(enriched),
        "clusters":      len(clusters),
        "wallets_added": wallets_added,
    }


def analyze_all_flagged() -> list[dict]:
    results = []
    for addr, info in list(store.tokens.items()):
        if info.get("flagged") and info.get("analysis_status") != "complete":
            results.append(analyze_token(addr))
    return results
