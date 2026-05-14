"""
scanner/cex_filter.py
──────────────────────
Filters token candidates to only those listed on centralized exchanges
(primarily Bitget, plus Bybit, OKX, Binance as fallback).

Why: Pump/dump schemes on CEX-listed tokens are more meaningful because:
  1. The token has real liquidity and a real price on CEX
  2. Manipulators can long on CEX futures while dumping on-chain
  3. You can actually trade the signal on a CEX futures market

Uses CoinGecko free API to check exchange listings.
No API key needed.
"""

import time
import requests
from functools import lru_cache

COINGECKO_COIN_LIST   = "https://api.coingecko.com/api/v3/coins/list?include_platform=true"
COINGECKO_COIN_DETAIL = "https://api.coingecko.com/api/v3/coins/{coin_id}?tickers=true&market_data=false&community_data=false&developer_data=false"
COINGECKO_CONTRACT    = "https://api.coingecko.com/api/v3/coins/ethereum/contract/{address}"

# CEXs we care about — token must be on at least one
TARGET_EXCHANGES = {
    "bitget",
    "bybit",
    "okx",
    "binance",
    "kucoin",
    "gate",
    "mexc",
}

# Cache the full coin list (it's large, only fetch once per session)
_coin_list_cache: list[dict] = []
_address_to_id:   dict[str, str] = {}


def _get(url: str) -> dict | list | None:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "sentinel/2.0"})
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[CEXFilter] GET error: {e}")
    return None


def _load_coin_list():
    """Load the full CoinGecko coin list and build address → coin_id map."""
    global _coin_list_cache, _address_to_id
    if _address_to_id:
        return  # already loaded

    print("[CEXFilter] Loading CoinGecko coin list (one-time)...")
    data = _get(COINGECKO_COIN_LIST)
    if not data:
        print("[CEXFilter] ⚠️  Could not load coin list")
        return

    _coin_list_cache = data
    for coin in data:
        platforms = coin.get("platforms") or {}
        eth_addr  = platforms.get("ethereum", "")
        if eth_addr:
            _address_to_id[eth_addr.lower()] = coin["id"]

    print(f"[CEXFilter] Loaded {len(_address_to_id)} Ethereum tokens from CoinGecko")


def get_coin_id(token_address: str) -> str | None:
    """Resolve an Ethereum contract address to a CoinGecko coin ID."""
    _load_coin_list()
    return _address_to_id.get(token_address.lower())


def get_exchanges_for_token(token_address: str) -> tuple[list[str], dict]:
    """
    Returns (list_of_exchange_ids, coin_meta) for a token address.
    Hits CoinGecko contract endpoint which returns tickers.
    """
    data = _get(COINGECKO_CONTRACT.format(address=token_address.lower()))
    if not data:
        return [], {}

    tickers   = data.get("tickers") or []
    exchanges = []
    for t in tickers:
        market = (t.get("market") or {}).get("identifier", "").lower()
        if market:
            exchanges.append(market)

    meta = {
        "coin_id":     data.get("id", ""),
        "name":        data.get("name", ""),
        "symbol":      data.get("symbol", "").upper(),
        "market_cap":  (data.get("market_data") or {}).get("market_cap", {}).get("usd"),
        "exchanges":   list(set(exchanges)),
    }
    return list(set(exchanges)), meta


def is_on_target_cex(token_address: str) -> tuple[bool, list[str], dict]:
    """
    Main filter function.
    Returns (is_listed, matched_exchanges, coin_meta).

    A token passes if it's listed on at least one TARGET_EXCHANGE.
    """
    time.sleep(0.3)  # gentle rate limiting on free CoinGecko tier

    exchanges, meta = get_exchanges_for_token(token_address)
    if not exchanges:
        # Fallback: try via coin ID
        coin_id = get_coin_id(token_address)
        if coin_id:
            detail = _get(COINGECKO_COIN_DETAIL.format(coin_id=coin_id))
            if detail:
                tickers = detail.get("tickers") or []
                exchanges = list(set(
                    (t.get("market") or {}).get("identifier", "").lower()
                    for t in tickers
                ))
                meta = {
                    "coin_id": coin_id,
                    "name":    detail.get("name", ""),
                    "symbol":  detail.get("symbol", "").upper(),
                    "exchanges": exchanges,
                }

    matched = [e for e in exchanges if e in TARGET_EXCHANGES]
    is_listed = len(matched) > 0

    return is_listed, matched, meta


def filter_tokens(candidates: list[dict]) -> list[dict]:
    """
    Filter a list of token dicts to only those on a target CEX.
    Enriches each token with CEX listing metadata.
    Returns filtered list.
    """
    print(f"\n[CEXFilter] Filtering {len(candidates)} candidates against CEX listings...")
    passed = []

    for token in candidates:
        addr   = token.get("address", "")
        symbol = token.get("symbol", "?")

        if not addr:
            continue

        listed, matched_cexs, meta = is_on_target_cex(addr)

        if listed:
            enriched = {
                **token,
                "symbol":       meta.get("symbol") or symbol,
                "name":         meta.get("name", ""),
                "coin_id":      meta.get("coin_id", ""),
                "cex_listings": matched_cexs,
                "all_exchanges": meta.get("exchanges", []),
                "on_bitget":    "bitget" in matched_cexs,
                "on_binance":   "binance" in matched_cexs,
            }
            passed.append(enriched)
            cex_str = ", ".join(matched_cexs[:4])
            print(f"[CEXFilter] ✅ PASS  {symbol:12s} ({addr[:10]}...) — listed on: {cex_str}")
        else:
            print(f"[CEXFilter] ✗  SKIP  {symbol:12s} ({addr[:10]}...) — not on any target CEX")

    print(f"[CEXFilter] {len(passed)}/{len(candidates)} tokens passed CEX filter\n")
    return passed
