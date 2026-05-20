"""
analyzer/history.py  (v2 — ETH + Base)
────────────────────────────────────────
Downloads ERC-20 transfer history for a token around pump/dump windows.
Fetches gas details for each transfer.

Chain routing:
  ethereum → Etherscan   (api.etherscan.io)
  base     → Basescan    (api.basescan.org)

Both APIs share the identical Etherscan-style interface so the same
code works for both — only the base URL and API key differ.
"""

import os
import time
import requests
from datetime import datetime, timedelta
from typing import Optional

ETHERSCAN_BASE = "https://api.etherscan.io/api"
BASESCAN_BASE  = "https://api.basescan.org/api"
GWEI = 1e9

ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY", "")   # same key works for Basescan


def _api_base(chain: str) -> tuple[str, str]:
    """Return (api_base_url, api_key) for the given chain."""
    if chain == "base":
        return BASESCAN_BASE, ETHERSCAN_KEY
    return ETHERSCAN_BASE, ETHERSCAN_KEY


def _scan(chain: str, params: dict) -> list | dict | None:
    base_url, api_key = _api_base(chain)
    params["apikey"] = api_key
    try:
        r = requests.get(base_url, params=params, timeout=15)
        d = r.json()
        if d.get("status") == "1":
            return d.get("result", [])
        return []
    except Exception as e:
        print(f"[History] {chain} scan error: {e}")
        return None


def get_block_at_timestamp(ts: int, chain: str) -> Optional[int]:
    """Convert unix timestamp to nearest block number on the given chain."""
    params = {
        "module":    "block",
        "action":    "getblocknobytime",
        "timestamp": ts,
        "closest":   "before",
    }
    result = _scan(chain, params)
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
    api_key: str,           # kept for backward compat — ignored, chain-routed internally
    padding_hours: int = 24,
    chain: str = "ethereum",
) -> list[dict]:
    """
    Download all ERC-20 transfers for a token in a time window on the given chain.
    """
    try:
        start_dt = datetime.fromisoformat(window_start) - timedelta(hours=padding_hours)
        end_dt   = datetime.fromisoformat(window_end)   + timedelta(hours=padding_hours)
    except Exception:
        start_dt = datetime.utcnow() - timedelta(days=3)
        end_dt   = datetime.utcnow()

    start_ts = int(start_dt.timestamp())
    end_ts   = int(end_dt.timestamp())

    print(f"[History] [{chain}] Fetching transfers {start_dt.date()} → {end_dt.date()}")

    start_block = get_block_at_timestamp(start_ts, chain) or 0
    end_block   = get_block_at_timestamp(end_ts,   chain) or 99999999
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
    transfers = _scan(chain, params) or []
    print(f"[History] [{chain}] Downloaded {len(transfers)} transfers")
    return transfers


def enrich_with_gas(transfers: list[dict], api_key: str,
                    chain: str = "ethereum") -> list[dict]:
    """
    Fetch full tx details for gas settings.
    Rate-limited to respect free tier (5 calls/sec Etherscan, 5/sec Basescan).
    """
    enriched = []
    total    = len(transfers)

    for i, tx in enumerate(transfers):
        tx_hash = tx.get("hash", "")
        if not tx_hash:
            continue

        params = {
            "module": "proxy",
            "action": "eth_getTransactionByHash",
            "txhash": tx_hash,
        }
        detail = _scan(chain, params)

        gas_limit        = 0
        max_priority_fee = 0.0
        max_fee          = 0.0

        if detail and isinstance(detail, dict):
            try:
                gas_limit        = int(detail.get("gas", "0x0"), 16)
                max_priority_fee = int(detail.get("maxPriorityFeePerGas", "0x0"), 16) / GWEI
                max_fee          = int(detail.get("maxFeePerGas",         "0x0"), 16) / GWEI
            except Exception:
                pass

        enriched.append({
            **tx,
            "gas_limit":        gas_limit,
            "max_priority_fee": round(max_priority_fee, 4),
            "max_fee":          round(max_fee, 4),
            "chain":            chain,
        })

        if (i + 1) % 10 == 0:
            print(f"[History] [{chain}] Enriched {i+1}/{total} txs...")
            time.sleep(0.25)

    print(f"[History] [{chain}] Enrichment complete: {len(enriched)} txs")
    return enriched
