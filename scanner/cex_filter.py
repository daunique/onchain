"""
scanner/cex_filter.py
──────────────────────
Filters token candidates to only those listed on Bitget or other major CEXs.

Fast path: CoinGecko /coins/markets endpoint returns exchange data in bulk.
We pre-load a set of known CEX-listed Ethereum token addresses once per session,
then filter against that set instantly — no per-token API calls needed.
"""

import time
import requests

TARGET_EXCHANGES = {"bitget", "bybit", "okx", "binance", "kucoin", "gate", "mexc"}

# Cache: set of lowercase Ethereum contract addresses confirmed on a target CEX
_cex_address_cache: set[str] = set()
_cache_loaded = False

COINGECKO_MARKETS = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&category=ethereum-ecosystem"
    "&order=volume_desc&per_page=250&page={page}"
    "&sparkline=false"
)
COINGECKO_COIN = "https://api.coingecko.com/api/v3/coins/{coin_id}?tickers=true&market_data=false&community_data=false&developer_data=false&sparkline=false"
COINGECKO_CONTRACT = "https://api.coingecko.com/api/v3/coins/ethereum/contract/{address}"


def _get(url: str, timeout: int = 12) -> dict | list | None:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "sentinel/2.0"})
        if r.ok:
            return r.json()
        print(f"[CEXFilter] HTTP {r.status_code} for {url[:70]}")
    except Exception as e:
        print(f"[CEXFilter] Error: {e}")
    return None


def _load_cex_cache():
    """
    Pre-load top 250 high-volume Ethereum tokens from CoinGecko markets,
    then check their tickers for CEX listings.
    Stores confirmed addresses in _cex_address_cache.
    """
    global _cache_loaded
    if _cache_loaded:
        return

    print("[CEXFilter] Loading CEX-listed token cache from CoinGecko...")

    # Step 1: get top 250 ETH ecosystem coins by volume
    coins = _get(COINGECKO_MARKETS.format(page=1)) or []
    if not coins:
        print("[CEXFilter] Could not load markets — will do per-token checks")
        _cache_loaded = True
        return

    print(f"[CEXFilter] Got {len(coins)} coins from markets endpoint")

    # Step 2: for each coin, check if it has a known CEX listing
    # We use a quick heuristic: coins with market_cap > $1M are almost certainly
    # on at least one major CEX. We confirm via tickers for a sample.
    confirmed = 0
    for coin in coins[:50]:  # check top 50 by volume for tickers
        coin_id = coin.get("id", "")
        if not coin_id:
            continue
        detail = _get(COINGECKO_COIN.format(coin_id=coin_id))
        if not detail:
            time.sleep(0.5)
            continue
        tickers   = detail.get("tickers") or []
        platforms = (detail.get("platforms") or {})
        eth_addr  = platforms.get("ethereum", "").lower()
        exchanges = {
            (t.get("market") or {}).get("identifier", "").lower()
            for t in tickers
        }
        if exchanges & TARGET_EXCHANGES and eth_addr:
            _cex_address_cache.add(eth_addr)
            confirmed += 1
        time.sleep(0.25)

    print(f"[CEXFilter] Cache loaded: {confirmed} CEX-listed ETH tokens confirmed")
    _cache_loaded = True


def check_token(address: str) -> tuple[bool, list[str], dict]:
    """
    Check a single token address against CEX listings.
    Returns (is_listed, matched_exchanges, meta).
    Fast path: check cache first.
    Slow path: direct CoinGecko contract lookup.
    """
    addr = address.lower()

    # Fast path
    if addr in _cex_address_cache:
        return True, ["cached_cex_match"], {"address": addr}

    # Slow path: direct contract lookup
    data = _get(COINGECKO_CONTRACT.format(address=addr))
    if not data:
        return False, [], {}

    tickers   = data.get("tickers") or []
    platforms = data.get("platforms") or {}
    exchanges = list({
        (t.get("market") or {}).get("identifier", "").lower()
        for t in tickers
    })
    matched = [e for e in exchanges if e in TARGET_EXCHANGES]

    meta = {
        "coin_id":     data.get("id", ""),
        "name":        data.get("name", ""),
        "symbol":      (data.get("symbol") or "").upper(),
        "cex_listings": matched,
        "on_bitget":   "bitget" in matched,
        "on_binance":  "binance" in matched,
    }

    if matched:
        _cex_address_cache.add(addr)

    return bool(matched), matched, meta


def filter_tokens(candidates: list[dict]) -> list[dict]:
    """
    Filter candidates to CEX-listed only.
    Tries cache first, then per-token lookup with timeout guard.
    """
    _load_cex_cache()

    print(f"\n[CEXFilter] Filtering {len(candidates)} candidates...")
    passed = []

    for token in candidates:
        addr   = token.get("address", "").lower()
        symbol = token.get("symbol", "?")
        if not addr:
            continue

        try:
            listed, matched, meta = check_token(addr)
        except Exception as e:
            print(f"[CEXFilter] Error checking {symbol}: {e}")
            listed, matched, meta = False, [], {}

        if listed:
            enriched = {
                **token,
                "symbol":       meta.get("symbol") or symbol,
                "name":         meta.get("name", ""),
                "coin_id":      meta.get("coin_id", ""),
                "cex_listings": matched,
                "on_bitget":    "bitget" in matched,
                "on_binance":   "binance" in matched,
            }
            passed.append(enriched)
            cex_str = ", ".join(matched[:3]) or "cached"
            print(f"[CEXFilter] PASS  {symbol:12s} — {cex_str}")
        else:
            print(f"[CEXFilter] SKIP  {symbol:12s} — not on target CEX")

        time.sleep(0.3)

    print(f"[CEXFilter] {len(passed)}/{len(candidates)} passed\n")
    return passed
