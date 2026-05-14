"""
analyzer/history.py
────────────────────
Downloads full ERC-20 transfer history for a token around pump/dump windows.
Fetches complete tx details (gas settings) for each transfer.
"""

import os
import time
import requests
from datetime import datetime, timedelta
from typing import Optional

ETHERSCAN_BASE = "https://api.etherscan.io/api"
GWEI = 1e9


def _etherscan(params: dict, api_key: str) -> list | dict | None:
    params["apikey"] = api_key
    try:
        r = requests.get(ETHERSCAN_BASE, params=params, timeout=15)
        d = r.json()
        if d.get("status") == "1":
            return d.get("result", [])
        # status 0 with empty result is fine (no txs)
        return []
    except Exception as e:
        print(f"[History] Etherscan error: {e}")
        return None


def get_block_at_timestamp(ts: int, api_key: str) -> Optional[int]:
    """Convert a unix timestamp to the nearest block number."""
    params = {
        "module":    "block",
        "action":    "getblocknobytime",
        "timestamp": ts,
        "closest":   "before",
    }
    result = _etherscan(params, api_key)
    if result and isinstance(result, (str, int)):
        try:
            return int(result)
        except Exception:
            pass
    return None


def fetch_transfers_for_window(
    token_address: str,
    window_start: str,
    window_end: str,
    api_key: str,
    padding_hours: int = 24,
) -> list[dict]:
    """
    Download all ERC-20 transfers for a token in a time window
    (with padding before/after to catch pre-positioning and exit moves).
    """
    try:
        start_dt = datetime.fromisoformat(window_start) - timedelta(hours=padding_hours)
        end_dt   = datetime.fromisoformat(window_end)   + timedelta(hours=padding_hours)
    except Exception:
        start_dt = datetime.utcnow() - timedelta(days=3)
        end_dt   = datetime.utcnow()

    start_ts = int(start_dt.timestamp())
    end_ts   = int(end_dt.timestamp())

    print(f"[History] Fetching transfers {start_dt.date()} → {end_dt.date()}")

    # Convert timestamps to block numbers
    start_block = get_block_at_timestamp(start_ts, api_key) or 0
    end_block   = get_block_at_timestamp(end_ts,   api_key) or 99999999
    time.sleep(0.25)

    params = {
        "module":          "account",
        "action":          "tokentx",
        "contractaddress": token_address,
        "startblock":      start_block,
        "endblock":        end_block,
        "page":            1,
        "offset":          2000,
        "sort":            "asc",
    }
    transfers = _etherscan(params, api_key) or []
    print(f"[History] Downloaded {len(transfers)} transfers")
    return transfers


def enrich_with_gas(transfers: list[dict], api_key: str) -> list[dict]:
    """
    For each transfer, fetch the full transaction to get gas settings.
    Returns enriched list. Rate-limited to respect free Etherscan tier.
    """
    enriched = []
    total = len(transfers)

    for i, tx in enumerate(transfers):
        tx_hash = tx.get("hash", "")
        if not tx_hash:
            continue

        params = {
            "module": "proxy",
            "action": "eth_getTransactionByHash",
            "txhash": tx_hash,
        }
        detail = _etherscan(params, api_key)

        gas_limit        = 0
        max_priority_fee = 0.0
        max_fee          = 0.0

        if detail and isinstance(detail, dict):
            try:
                gas_limit        = int(detail.get("gas", "0x0"), 16)
                max_priority_fee = int(detail.get("maxPriorityFeePerGas", "0x0"), 16) / GWEI
                max_fee          = int(detail.get("maxFeePerGas", "0x0"), 16) / GWEI
            except Exception:
                pass

        enriched.append({
            **tx,
            "gas_limit":        gas_limit,
            "max_priority_fee": round(max_priority_fee, 4),
            "max_fee":          round(max_fee, 4),
        })

        if (i + 1) % 10 == 0:
            print(f"[History] Enriched {i+1}/{total} txs...")
            time.sleep(0.25)  # 4 calls/sec — safe on free tier

    print(f"[History] Enrichment complete: {len(enriched)} txs")
    return enriched
