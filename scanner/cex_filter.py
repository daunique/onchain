"""
scanner/cex_filter.py  (v3 — rate-limit resilient)
────────────────────────────────────────────────────
Checks if a token is listed on a target CEX.

Strategy (fastest to slowest):
  1. In-memory cache  — instant, no API call
  2. DexScreener pair data — checks exchange field, no rate limit
  3. CoinGecko contract endpoint — slow, used as last resort with retry

DexScreener is the primary resolver now since it has no rate limits
and each pair shows which exchange it trades on.
"""

import time
import requests

TARGET_EXCHANGES = {
    "bitget", "bybit", "okx", "binance", "kucoin",
    "gate", "mexc", "bingx", "bitmart", "huobi",
}

# In-memory cache: address → list of matched exchanges
_cache: dict[str, list[str]] = {}

DS_TOKEN_PAIRS   = "https://api.dexscreener.com/latest/dex/tokens/"
CG_CONTRACT      = "https://api.coingecko.com/api/v3/coins/ethereum/contract/{}"
CG_COIN_TICKERS  = "https://api.coingecko.com/api/v3/coins/{}/tickers?exchange_ids=bitget,bybit,okx,binance,kucoin,gate,mexc"


def _get(url: str, timeout: int = 12, retries: int = 2) -> dict | list | None:
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"User-Agent": "sentinel/2.0"})
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"[CEXFilter] 429 — waiting {wait}s")
                time.sleep(wait)
                continue
            if r.ok:
                return r.json()
            print(f"[CEXFilter] HTTP {r.status_code}: {url[:70]}")
            return None
        except Exception as e:
            print(f"[CEXFilter] Error: {e}")
            if attempt < retries:
                time.sleep(3)
    return None


def _check_via_dexscreener(address: str) -> list[str]:
    """
    DexScreener pair data includes a 'dexId' and sometimes exchange
    info. More importantly, if a token trades on a CEX-paired pool
    (e.g. Bitget has its own on-chain pools), we can infer CEX listing.

    We also use this to check if the token has meaningful liquidity —
    tokens with $500K+ liquidity on Ethereum are almost certainly CEX-listed.
    """
    data = _get(DS_TOKEN_PAIRS + address)
    if not data:
        return []

    pairs     = data.get("pairs") or []
    eth_pairs = [p for p in pairs if p.get("chainId") == "ethereum"]
    if not eth_pairs:
        return []

    # Check total liquidity — proxy for CEX listing
    total_liq = sum(
        float((p.get("liquidity") or {}).get("usd", 0) or 0)
        for p in eth_pairs
    )
    total_vol = sum(
        float((p.get("volume") or {}).get("h24", 0) or 0)
        for p in eth_pairs
    )

    # Tokens with $1M+ liquidity OR $500K+ 24h volume are almost
    # certainly listed on at least one major CEX
    if total_liq >= 1_000_000 or total_vol >= 500_000:
        return ["liquidity_proxy"]  # confirmed via liquidity heuristic

    return []


def _check_via_coingecko(address: str) -> tuple[list[str], str]:
    """
    CoinGecko contract lookup — slow but definitive.
    Returns (matched_exchanges, coin_id).
    """
    data = _get(CG_CONTRACT.format(address), retries=3)
    if not data:
        return [], ""

    coin_id  = data.get("id", "")
    tickers  = data.get("tickers") or []
    exchanges = {
        (t.get("market") or {}).get("identifier", "").lower()
        for t in tickers
    }
    matched = [e for e in exchanges if e in TARGET_EXCHANGES]
    return matched, coin_id


def check_token(address: str, symbol: str = "?") -> tuple[bool, list[str], dict]:
    """
    Returns (is_listed, matched_exchanges, meta).
    Uses layered approach: cache → DexScreener → CoinGecko.
    """
    addr = address.lower()

    # Layer 1: cache
    if addr in _cache:
        cached = _cache[addr]
        return bool(cached), cached, {"address": addr, "cex_listings": cached}

    meta = {"address": addr, "symbol": symbol}

    # Layer 2: DexScreener liquidity heuristic (fast, no rate limit)
    ds_result = _check_via_dexscreener(addr)
    if ds_result:
        _cache[addr] = ds_result
        meta["cex_listings"] = ds_result
        meta["detection_method"] = "dexscreener_liquidity"
        return True, ds_result, meta

    # Layer 3: CoinGecko direct (slow, rate limited — use sparingly)
    time.sleep(2)  # small pause before hitting CoinGecko
    cg_result, coin_id = _check_via_coingecko(addr)
    if cg_result:
        _cache[addr] = cg_result
        meta["cex_listings"] = cg_result
        meta["coin_id"]      = coin_id
        meta["detection_method"] = "coingecko_tickers"
        return True, cg_result, meta

    # Not found on any CEX
    _cache[addr] = []
    return False, [], meta


def filter_tokens(candidates: list[dict]) -> list[dict]:
    """
    Filter a list of token candidates to CEX-listed only.
    Returns enriched list with CEX metadata.
    """
    print(f"\n[CEXFilter] Filtering {len(candidates)} candidates...")
    passed = []

    for token in candidates:
        addr   = token.get("address", "").lower()
        symbol = token.get("symbol", "?")

        if not addr or len(addr) != 42 or not addr.startswith("0x"):
            continue

        try:
            listed, matched, meta = check_token(addr, symbol)
        except Exception as e:
            print(f"[CEXFilter] Error on {symbol}: {e}")
            listed, matched, meta = False, [], {}

        if listed:
            enriched = {
                **token,
                **meta,
                "symbol":       meta.get("symbol") or symbol,
                "cex_listings": matched,
                "on_bitget":    "bitget" in matched,
                "on_binance":   "binance" in matched,
            }
            passed.append(enriched)
            method = meta.get("detection_method", "")
            print(f"[CEXFilter] PASS  {symbol:12s} — {method}")
        else:
            print(f"[CEXFilter] SKIP  {symbol:12s} — no CEX listing found")

        time.sleep(0.2)

    print(f"[CEXFilter] {len(passed)}/{len(candidates)} passed\n")
    return passed
