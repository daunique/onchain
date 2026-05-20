"""
scanner/cex_filter.py  (v5 — ETH + Base, DexScreener only)
────────────────────────────────────────────────────────────
CEX listing is determined via DexScreener liquidity/volume thresholds.
No CoinGecko calls = no rate limiting.

Works identically for Ethereum and Base — DexScreener covers both chains
via the same /latest/dex/tokens/<address> endpoint, returning pairs
across all chains that token trades on.

Cache is keyed by chain:address so the same contract address on ETH
and Base are treated as distinct assets.
"""

import time
import sys
import requests
from datetime import datetime, timedelta

MIN_LIQUIDITY_USD = float(100_000)   # $100K
MIN_VOLUME_24H    = float(50_000)    # $50K

DS_TOKEN_PAIRS = "https://api.dexscreener.com/latest/dex/tokens/"

# (chain, address) → {passed, checked_at, reason}
_cache: dict[str, dict] = {}
CACHE_TTL_HOURS = 6


def _get(url: str, timeout: int = 10) -> dict | None:
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "sentinel/3.0"})
        if r.ok:
            return r.json()
        if r.status_code != 404:
            print(f"[CEXFilter] HTTP {r.status_code}: {url[:65]}")
    except Exception as e:
        print(f"[CEXFilter] Request error: {e}")
    return None


def _is_cache_valid(entry: dict) -> bool:
    try:
        checked = datetime.fromisoformat(entry["checked_at"])
        return (datetime.utcnow() - checked).total_seconds() < CACHE_TTL_HOURS * 3600
    except Exception:
        return False


def check_token(address: str, chain: str = "ethereum",
                symbol: str = "?") -> tuple[bool, str]:
    """
    Returns (passes, reason).
    chain should be 'ethereum' or 'base' (DexScreener chain IDs).
    Checks liquidity/volume on the specified chain only.
    """
    addr     = address.lower()
    cache_key = f"{chain}:{addr}"

    if cache_key in _cache and _is_cache_valid(_cache[cache_key]):
        entry = _cache[cache_key]
        return entry["passed"], entry["reason"]

    data = _get(DS_TOKEN_PAIRS + addr)
    if not data:
        _cache[cache_key] = {"passed": False, "reason": "no_data",
                             "checked_at": datetime.utcnow().isoformat()}
        return False, "no_data"

    pairs = data.get("pairs") or []

    # Filter to the specific chain this candidate came from
    chain_pairs = [p for p in pairs if p.get("chainId") == chain]

    if not chain_pairs:
        reason = f"not_on_{chain}"
        _cache[cache_key] = {"passed": False, "reason": reason,
                             "checked_at": datetime.utcnow().isoformat()}
        return False, reason

    total_liq = sum(
        float((p.get("liquidity") or {}).get("usd", 0) or 0)
        for p in chain_pairs
    )
    total_vol = sum(
        float((p.get("volume") or {}).get("h24", 0) or 0)
        for p in chain_pairs
    )

    if total_liq >= MIN_LIQUIDITY_USD:
        reason = f"liq=${total_liq/1e6:.2f}M on {chain}"
        _cache[cache_key] = {"passed": True, "reason": reason,
                             "checked_at": datetime.utcnow().isoformat()}
        return True, reason

    if total_vol >= MIN_VOLUME_24H:
        reason = f"vol=${total_vol/1e3:.0f}K on {chain}"
        _cache[cache_key] = {"passed": True, "reason": reason,
                             "checked_at": datetime.utcnow().isoformat()}
        return True, reason

    reason = f"liq=${total_liq/1e3:.0f}K vol=${total_vol/1e3:.0f}K on {chain} (below threshold)"
    _cache[cache_key] = {"passed": False, "reason": reason,
                         "checked_at": datetime.utcnow().isoformat()}
    return False, reason


def filter_tokens(candidates: list[dict]) -> list[dict]:
    """
    Filter a mixed ETH+Base candidate list.
    Each candidate must have a 'chain' field ('ethereum' or 'base').
    """
    print(f"\n[CEXFilter] Filtering {len(candidates)} candidates "
          f"(cache: {len(_cache)} entries)...")
    sys.stdout.flush()

    passed = []

    for token in candidates:
        addr   = token.get("address", "").lower()
        chain  = token.get("chain", "ethereum")
        symbol = token.get("symbol", "?")

        if not addr or len(addr) != 42 or not addr.startswith("0x"):
            continue

        ok, reason = check_token(addr, chain, symbol)

        if ok:
            passed.append({
                **token,
                "cex_listings":     ["dexscreener_verified"],
                "detection_method": "dexscreener_liquidity",
                "detection_reason": reason,
            })
            print(f"[CEXFilter] PASS  {symbol:12s} [{chain:8s}]  {reason}")
        else:
            print(f"[CEXFilter] SKIP  {symbol:12s} [{chain:8s}]  {reason}")

        time.sleep(0.3)
        sys.stdout.flush()

    print(f"[CEXFilter] {len(passed)}/{len(candidates)} passed\n")
    sys.stdout.flush()
    return passed
