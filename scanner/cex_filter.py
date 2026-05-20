"""
scanner/cex_filter.py  (v4 — DexScreener only, no CoinGecko)
──────────────────────────────────────────────────────────────
CEX listing is determined purely via DexScreener liquidity/volume
thresholds. No CoinGecko calls = no rate limiting = instant filtering.

Logic:
  Any ETH token with $1M+ liquidity OR $500K+ 24h volume is
  almost certainly listed on a major CEX. This is true for
  99% of tokens we care about. The rare exception (a token
  with huge on-chain volume but no CEX listing) is not the
  target of this system anyway.

Cache persists across scan cycles so repeated tokens are free.
"""

import time
import sys
import requests
from datetime import datetime, timedelta

# Thresholds for "this token is CEX-listed"
MIN_LIQUIDITY_USD = float(500_000)   # $500K liquidity
MIN_VOLUME_24H    = float(200_000)   # $200K 24h volume

DS_TOKEN_PAIRS = "https://api.dexscreener.com/latest/dex/tokens/"

# address → {"passed": bool, "checked_at": str, "reason": str}
_cache: dict[str, dict] = {}
CACHE_TTL_HOURS = 6  # recheck after 6 hours


def _get(url: str, timeout: int = 10) -> dict | None:
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "sentinel/2.0"})
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


def check_token(address: str, symbol: str = "?") -> tuple[bool, str]:
    """
    Returns (passes, reason).
    Uses DexScreener liquidity/volume — no CoinGecko, no rate limits.
    """
    addr = address.lower()

    # Cache hit
    if addr in _cache and _is_cache_valid(_cache[addr]):
        entry = _cache[addr]
        return entry["passed"], entry["reason"]

    # DexScreener lookup
    data = _get(DS_TOKEN_PAIRS + addr)
    if not data:
        _cache[addr] = {"passed": False, "reason": "no_data",
                        "checked_at": datetime.utcnow().isoformat()}
        return False, "no_data"

    pairs     = data.get("pairs") or []
    eth_pairs = [p for p in pairs if p.get("chainId") == "ethereum"]

    if not eth_pairs:
        _cache[addr] = {"passed": False, "reason": "not_on_eth",
                        "checked_at": datetime.utcnow().isoformat()}
        return False, "not_on_eth"

    total_liq = sum(
        float((p.get("liquidity") or {}).get("usd", 0) or 0)
        for p in eth_pairs
    )
    total_vol = sum(
        float((p.get("volume") or {}).get("h24", 0) or 0)
        for p in eth_pairs
    )

    if total_liq >= MIN_LIQUIDITY_USD:
        reason = f"liq=${total_liq/1e6:.1f}M"
        _cache[addr] = {"passed": True, "reason": reason,
                        "checked_at": datetime.utcnow().isoformat()}
        return True, reason

    if total_vol >= MIN_VOLUME_24H:
        reason = f"vol=${total_vol/1e3:.0f}K"
        _cache[addr] = {"passed": True, "reason": reason,
                        "checked_at": datetime.utcnow().isoformat()}
        return True, reason

    reason = f"liq=${total_liq/1e3:.0f}K vol=${total_vol/1e3:.0f}K (below threshold)"
    _cache[addr] = {"passed": False, "reason": reason,
                    "checked_at": datetime.utcnow().isoformat()}
    return False, reason


def filter_tokens(candidates: list[dict]) -> list[dict]:
    """
    Filter candidates to CEX-listed only using DexScreener only.
    Fast — typically 0.3–0.5s per token, no rate limiting.
    """
    print(f"\n[CEXFilter] Filtering {len(candidates)} candidates "
          f"(cache: {len(_cache)} entries)...")
    sys.stdout.flush()

    passed = []

    for token in candidates:
        addr   = token.get("address", "").lower()
        symbol = token.get("symbol", "?")

        if not addr or len(addr) != 42 or not addr.startswith("0x"):
            continue

        ok, reason = check_token(addr, symbol)

        if ok:
            passed.append({
                **token,
                "cex_listings":       ["dexscreener_verified"],
                "on_bitget":          False,  # unknown without CoinGecko
                "on_binance":         False,
                "detection_method":   "dexscreener_liquidity",
                "detection_reason":   reason,
            })
            print(f"[CEXFilter] PASS  {symbol:12s}  {reason}")
        else:
            print(f"[CEXFilter] SKIP  {symbol:12s}  {reason}")

        # Small polite delay — DexScreener has no official rate limit
        # but this keeps us from hammering it
        time.sleep(0.3)
        sys.stdout.flush()

    print(f"[CEXFilter] {len(passed)}/{len(candidates)} passed\n")
    sys.stdout.flush()
    return passed
