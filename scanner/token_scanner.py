"""
scanner/token_scanner.py  (v3 — robust multi-source)
──────────────────────────────────────────────────────
Discovers CEX-listed Ethereum tokens showing pump/dump patterns.

Sources (in order, all free, no key needed):
  A. DexScreener /token-boosts/top     — boosted tokens
  B. DexScreener /token-boosts/latest  — recently boosted
  C. DexScreener search queries        — high volume ETH pairs
  D. DexScreener /latest/dex/pairs/ethereum — top ETH pairs directly
  E. CoinGecko trending coins          — trending with ETH platform
  F. CoinGecko top gainers             — biggest movers (pump candidates)

All sources combined → deduplicated → CEX filtered → pump/dump detection.
"""

import os
import time
import requests
from datetime import datetime, timedelta
from data.store import store
from scanner.cex_filter import filter_tokens

MIN_PRICE_CHANGE_PCT = float(os.getenv("MIN_PRICE_CHANGE_PCT", 15))
MAX_TOKENS_TO_SCAN   = int(os.getenv("MAX_TOKENS_TO_SCAN", 20))

# DexScreener endpoints
DS_BOOSTS_TOP     = "https://api.dexscreener.com/token-boosts/top/v1"
DS_BOOSTS_LATEST  = "https://api.dexscreener.com/token-boosts/latest/v1"
DS_PAIRS_ETH      = "https://api.dexscreener.com/latest/dex/pairs/ethereum"
DS_SEARCH         = "https://api.dexscreener.com/latest/dex/search?q="
DS_TOKEN_PAIRS    = "https://api.dexscreener.com/latest/dex/tokens/"

# CoinGecko endpoints
CG_TRENDING       = "https://api.coingecko.com/api/v3/search/trending"
CG_TOP_GAINERS    = "https://api.coingecko.com/api/v3/coins/top_gainers_losers?vs_currency=usd&duration=24h&top_coins=500"
CG_MARKETS_ETH    = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&category=ethereum-ecosystem&order=volume_desc&per_page=100&page=1&price_change_percentage=1h,24h"


def _get(url: str, timeout: int = 12) -> dict | list | None:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "sentinel/2.0"})
        if r.ok:
            return r.json()
        print(f"[Scanner] HTTP {r.status_code}: {url[:70]}")
    except Exception as e:
        print(f"[Scanner] Error {url[:60]}: {e}")
    return None


# ── Source fetchers ────────────────────────────────────────────────────────────

def _from_dexscreener_boosts() -> list[dict]:
    results = []
    for url in [DS_BOOSTS_TOP, DS_BOOSTS_LATEST]:
        data = _get(url)
        if data and isinstance(data, list):
            for item in data:
                addr  = item.get("tokenAddress", "")
                chain = item.get("chainId", "")
                if addr and chain in ("ethereum", "eth"):
                    results.append({
                        "address": addr.lower(),
                        "symbol":  item.get("description", "?")[:20],
                        "source":  "dexscreener_boost",
                    })
        time.sleep(0.3)
    print(f"[Scanner] DexScreener boosts: {len(results)} ETH tokens")
    return results


def _from_dexscreener_search() -> list[dict]:
    results = []
    queries = [
        "ethereum",
        "eth token",
        "ethereum meme",
        "ethereum defi",
    ]
    for q in queries:
        data = _get(DS_SEARCH + q.replace(" ", "%20"))
        if data:
            for p in (data.get("pairs") or [])[:15]:
                if p.get("chainId") != "ethereum":
                    continue
                base = p.get("baseToken") or {}
                addr = base.get("address", "").lower()
                sym  = base.get("symbol", "?")
                if addr:
                    results.append({"address": addr, "symbol": sym, "source": "dexscreener_search"})
        time.sleep(0.3)
    print(f"[Scanner] DexScreener search: {len(results)} ETH tokens")
    return results


def _from_dexscreener_pairs() -> list[dict]:
    results = []
    data = _get(DS_PAIRS_ETH)
    if data:
        for p in (data.get("pairs") or [])[:30]:
            base = p.get("baseToken") or {}
            addr = base.get("address", "").lower()
            sym  = base.get("symbol", "?")
            if addr:
                results.append({"address": addr, "symbol": sym, "source": "dexscreener_pairs"})
    print(f"[Scanner] DexScreener pairs: {len(results)} ETH tokens")
    return results


def _from_coingecko_trending() -> list[dict]:
    results = []
    data = _get(CG_TRENDING)
    if data:
        for coin in data.get("coins", [])[:20]:
            item      = coin.get("item", {})
            platforms = item.get("platforms", {})
            eth_addr  = platforms.get("ethereum", "").lower()
            if eth_addr:
                results.append({
                    "address": eth_addr,
                    "symbol":  item.get("symbol", "?").upper(),
                    "source":  "coingecko_trending",
                })
    print(f"[Scanner] CoinGecko trending: {len(results)} ETH tokens")
    return results


def _from_coingecko_gainers() -> list[dict]:
    results = []
    data = _get(CG_TOP_GAINERS)
    if data:
        gainers = data.get("top_gainers") or []
        for coin in gainers[:20]:
            # top_gainers returns coin IDs, not addresses directly
            # we use the symbol and rely on CEX filter to resolve
            sym  = coin.get("symbol", "?").upper()
            addr = coin.get("contract_address", "").lower()
            if addr:
                results.append({"address": addr, "symbol": sym, "source": "coingecko_gainers"})
    print(f"[Scanner] CoinGecko gainers: {len(results)} ETH tokens")
    return results


def _from_coingecko_markets() -> list[dict]:
    """Top ETH ecosystem coins with high 1h/24h price change."""
    results = []
    data = _get(CG_MARKETS_ETH)
    if data and isinstance(data, list):
        for coin in data:
            platforms = coin.get("platforms") or {}
            eth_addr  = ""
            # markets endpoint doesn't always return platforms inline
            # but sometimes does — use what we have
            if isinstance(platforms, dict):
                eth_addr = platforms.get("ethereum", "").lower()

            # Even without address, store coin_id for CEX filter fallback
            change_1h  = coin.get("price_change_percentage_1h_in_currency") or 0
            change_24h = coin.get("price_change_percentage_24h") or 0

            if abs(change_1h) >= MIN_PRICE_CHANGE_PCT or abs(change_24h) >= MIN_PRICE_CHANGE_PCT:
                if eth_addr:
                    results.append({
                        "address":   eth_addr,
                        "symbol":    (coin.get("symbol") or "?").upper(),
                        "coin_id":   coin.get("id", ""),
                        "change_1h": change_1h,
                        "change_24h": change_24h,
                        "source":    "coingecko_markets",
                    })
    print(f"[Scanner] CoinGecko markets (moving): {len(results)} ETH tokens")
    return results


# ── Pump/dump detection ────────────────────────────────────────────────────────

def fetch_pair_data(token_address: str) -> dict | None:
    data = _get(DS_TOKEN_PAIRS + token_address)
    if not data:
        return None
    pairs     = data.get("pairs") or []
    eth_pairs = [p for p in pairs if p.get("chainId") == "ethereum"]
    if not eth_pairs:
        return None
    return max(eth_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))


def detect_pump_dump_windows(token_address: str, symbol: str, prefetched_changes: dict = None) -> list[dict]:
    """
    Detect pump/dump patterns. Uses prefetched_changes if available
    (from CoinGecko markets), otherwise fetches from DexScreener.
    """
    now = datetime.utcnow()

    if prefetched_changes:
        changes = prefetched_changes
        liq     = 0
        vol_24h = 0
    else:
        pair = fetch_pair_data(token_address)
        if not pair:
            return []
        changes = pair.get("priceChange") or {}
        liq     = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
        vol_24h = float((pair.get("volume") or {}).get("h24", 0) or 0)

    h1  = float(changes.get("h1",  0) or 0)
    h6  = float(changes.get("h6",  0) or 0)
    h24 = float(changes.get("h24", 0) or 0)
    d7  = float(changes.get("d7",  0) or 0) if "d7" in changes else None

    windows = []

    # Pattern A: sharp 1h pump, mostly reversed by 24h
    if h1 >= MIN_PRICE_CHANGE_PCT and h24 < (h1 * 0.5):
        windows.append({
            "token":         token_address.lower(),
            "symbol":        symbol,
            "type":          "short_pump_dump",
            "pump_pct":      round(h1, 2),
            "dump_pct":      round(h1 - h24, 2),
            "window_start":  (now - timedelta(hours=25)).isoformat(),
            "window_end":    now.isoformat(),
            "liquidity_usd": liq,
            "volume_h24":    vol_24h,
        })

    # Pattern B: 6h pump reversed by 24h
    if h6 >= MIN_PRICE_CHANGE_PCT and h24 < (h6 * 0.5):
        windows.append({
            "token":         token_address.lower(),
            "symbol":        symbol,
            "type":          "mid_pump_dump",
            "pump_pct":      round(h6, 2),
            "dump_pct":      round(h6 - h24, 2),
            "window_start":  (now - timedelta(hours=25)).isoformat(),
            "window_end":    now.isoformat(),
            "liquidity_usd": liq,
            "volume_h24":    vol_24h,
        })

    # Pattern C: sawtooth — big intraday moves but flat 7d
    if (d7 is not None and abs(d7) < 8
            and (abs(h24) >= MIN_PRICE_CHANGE_PCT or abs(h6) >= MIN_PRICE_CHANGE_PCT)):
        windows.append({
            "token":         token_address.lower(),
            "symbol":        symbol,
            "type":          "repeated_cycle",
            "pump_pct":      round(max(abs(h24), abs(h6)), 2),
            "dump_pct":      round(abs(d7), 2),
            "window_start":  (now - timedelta(days=8)).isoformat(),
            "window_end":    now.isoformat(),
            "liquidity_usd": liq,
            "volume_h24":    vol_24h,
        })

    return windows


# ── Main scan ──────────────────────────────────────────────────────────────────

def fetch_raw_candidates() -> list[dict]:
    """Pull candidates from all sources and deduplicate."""
    all_candidates = []

    all_candidates += _from_dexscreener_boosts()
    all_candidates += _from_dexscreener_pairs()
    all_candidates += _from_dexscreener_search()
    all_candidates += _from_coingecko_trending()
    all_candidates += _from_coingecko_gainers()
    all_candidates += _from_coingecko_markets()

    # Deduplicate by address
    seen, unique = set(), []
    for c in all_candidates:
        addr = c.get("address", "").lower()
        if addr and addr not in seen:
            seen.add(addr)
            unique.append(c)

    print(f"[Scanner] Total unique ETH candidates: {len(unique)}")
    return unique


def run_scan() -> list[str]:
    print(f"\n[Scanner] ══ Scan started {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ══")

    # 1. Gather candidates from all sources
    raw = fetch_raw_candidates()
    print(f"[Scanner] {len(raw)} raw candidates found")

    if not raw:
        print("[Scanner] No candidates — check network/API access")
        store.log_scan({"candidates_checked": 0, "cex_passed": 0,
                        "tokens_flagged": 0, "pump_events_found": 0})
        return []

    # 2. CEX filter
    cex_listed = filter_tokens(raw[:MAX_TOKENS_TO_SCAN * 3])
    print(f"[Scanner] {len(cex_listed)} tokens passed CEX filter")

    if not cex_listed:
        print("[Scanner] No CEX-listed tokens found this cycle")
        store.log_scan({"candidates_checked": len(raw), "cex_passed": 0,
                        "tokens_flagged": 0, "pump_events_found": 0})
        return []

    cex_listed   = cex_listed[:MAX_TOKENS_TO_SCAN]
    flagged      = []
    total_events = 0

    # 3. Pump/dump detection
    for token in cex_listed:
        addr   = token["address"]
        symbol = token.get("symbol", "?")

        # Skip if analyzed recently
        existing = store.get_token(addr)
        if existing.get("last_analyzed"):
            try:
                last = datetime.fromisoformat(existing["last_analyzed"])
                if (datetime.utcnow() - last).total_seconds() < 21600:
                    print(f"[Scanner] Skip {symbol} (<6h ago)")
                    continue
            except Exception:
                pass

        # Use prefetched changes if available (from CoinGecko markets source)
        prefetched = None
        if token.get("source") == "coingecko_markets":
            prefetched = {
                "h1":  token.get("change_1h", 0),
                "h24": token.get("change_24h", 0),
            }

        windows = detect_pump_dump_windows(addr, symbol, prefetched)

        store.upsert_token(addr, {
            **token,
            "flagged":      len(windows) > 0,
            "pump_windows": len(windows),
        })

        if windows:
            for w in windows:
                store.add_pump_event(w)
            flagged.append(addr)
            total_events += len(windows)
            cexs = ", ".join(token.get("cex_listings", [])[:3])
            print(f"[Scanner] FLAGGED {symbol:12s} — {len(windows)} window(s) | CEX: {cexs}")
        else:
            print(f"[Scanner] clean   {symbol:12s}")

        time.sleep(0.3)

    store.log_scan({
        "candidates_checked": len(raw),
        "cex_passed":         len(cex_listed),
        "tokens_flagged":     len(flagged),
        "pump_events_found":  total_events,
    })
    store.save()

    print(f"[Scanner] ══ Done: {len(flagged)}/{len(cex_listed)} flagged ══\n")
    return flagged
