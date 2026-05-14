"""
scanner/token_scanner.py  (v2 — CEX-filtered)
──────────────────────────────────────────────
Discovers tokens showing pump/dump behaviour AND are listed on a CEX
(Bitget, Bybit, OKX, Binance, etc.).

Pipeline:
  1. Fetch trending tokens from DexScreener + CoinGecko
  2. Filter to CEX-listed tokens only
  3. Detect pump/dump windows from price data
  4. Flag tokens with repeated cycles
  5. Hand off to analyzer
"""

import os
import time
import requests
from datetime import datetime, timedelta
from data.store import store
from scanner.cex_filter import filter_tokens

MIN_PRICE_CHANGE_PCT = float(os.getenv("MIN_PRICE_CHANGE_PCT", 15))
MIN_PUMP_EVENTS      = int(os.getenv("MIN_PUMP_EVENTS", 2))
MAX_TOKENS_TO_SCAN   = int(os.getenv("MAX_TOKENS_TO_SCAN", 20))

DEXSCREENER_TRENDING = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_PAIRS    = "https://api.dexscreener.com/latest/dex/tokens/"
DEXSCREENER_SEARCH   = "https://api.dexscreener.com/latest/dex/search?q="
COINGECKO_TRENDING   = "https://api.coingecko.com/api/v3/search/trending"


def _get(url: str) -> dict | list | None:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "sentinel/2.0"})
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[Scanner] GET error {url[:60]}: {e}")
    return None


def fetch_raw_candidates() -> list[dict]:
    """Pull Ethereum token candidates from multiple free sources."""
    candidates = []

    # Source A: DexScreener top boosted
    data = _get(DEXSCREENER_TRENDING)
    if data and isinstance(data, list):
        for item in data[:40]:
            addr  = item.get("tokenAddress", "")
            chain = item.get("chainId", "")
            if addr and chain in ("ethereum", "eth"):
                candidates.append({
                    "address": addr.lower(),
                    "symbol":  item.get("description", "?"),
                    "source":  "dexscreener_boost",
                })

    # Source B: CoinGecko trending
    data = _get(COINGECKO_TRENDING)
    if data:
        for coin in data.get("coins", [])[:20]:
            item      = coin.get("item", {})
            platforms = item.get("platforms", {})
            eth_addr  = platforms.get("ethereum", "")
            if eth_addr:
                candidates.append({
                    "address": eth_addr.lower(),
                    "symbol":  item.get("symbol", "?"),
                    "source":  "coingecko_trending",
                })

    # Source C: DexScreener high-volume ETH search
    for q in ["ethereum high volume", "ethereum trending"]:
        data = _get(DEXSCREENER_SEARCH + q.replace(" ", "%20"))
        if data:
            for p in (data.get("pairs") or [])[:10]:
                if p.get("chainId") != "ethereum":
                    continue
                base = p.get("baseToken") or {}
                addr = base.get("address", "")
                sym  = base.get("symbol", "?")
                if addr:
                    candidates.append({"address": addr.lower(), "symbol": sym, "source": "dexscreener_search"})

    # Deduplicate
    seen, unique = set(), []
    for c in candidates:
        if c["address"] and c["address"] not in seen:
            seen.add(c["address"])
            unique.append(c)

    print(f"[Scanner] {len(unique)} raw candidates before CEX filter")
    return unique


def fetch_pair_data(token_address: str) -> dict | None:
    """Get best ETH pair data from DexScreener."""
    data = _get(DEXSCREENER_PAIRS + token_address)
    if not data:
        return None
    pairs     = data.get("pairs") or []
    eth_pairs = [p for p in pairs if p.get("chainId") == "ethereum"]
    if not eth_pairs:
        return None
    return max(eth_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))


def detect_pump_dump_windows(token_address: str, symbol: str) -> list[dict]:
    """Detect pump/dump events from DexScreener price change data."""
    pair = fetch_pair_data(token_address)
    if not pair:
        return []

    changes = pair.get("priceChange") or {}
    volume  = pair.get("volume") or {}
    liq     = float((pair.get("liquidity") or {}).get("usd", 0) or 0)

    h1  = float(changes.get("h1",  0) or 0)
    h6  = float(changes.get("h6",  0) or 0)
    h24 = float(changes.get("h24", 0) or 0)
    d7  = float(changes.get("d7",  0) or 0) if "d7" in changes else None

    now     = datetime.utcnow()
    windows = []

    # Pattern A: Sharp 1h spike, mostly reversed by 24h
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
            "volume_h24":    float(volume.get("h24", 0) or 0),
        })

    # Pattern B: 6h pump mostly reversed by 24h
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
            "volume_h24":    float(volume.get("h24", 0) or 0),
        })

    # Pattern C: Sawtooth — big intraday moves but flat 7d (repeated cycle)
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
            "volume_h24":    float(volume.get("h24", 0) or 0),
        })

    return windows


def run_scan() -> list[str]:
    """
    Full scan cycle.
    1. Fetch raw candidates
    2. Filter to CEX-listed only (Bitget, Bybit, OKX, Binance…)
    3. Detect pump/dump patterns
    4. Return flagged token addresses for the analyzer.
    """
    print(f"\n[Scanner] ══ Scan started {datetime.utcnow().isoformat()} ══")

    raw        = fetch_raw_candidates()
    cex_listed = filter_tokens(raw[:MAX_TOKENS_TO_SCAN * 2])

    if not cex_listed:
        print("[Scanner] No CEX-listed candidates this cycle")
        store.log_scan({"candidates_checked": len(raw), "cex_passed": 0,
                        "tokens_flagged": 0, "pump_events_found": 0})
        return []

    cex_listed   = cex_listed[:MAX_TOKENS_TO_SCAN]
    flagged      = []
    total_events = 0

    for token in cex_listed:
        addr   = token["address"]
        symbol = token["symbol"]

        # Skip if analyzed recently
        existing = store.get_token(addr)
        if existing.get("last_analyzed"):
            try:
                last = datetime.fromisoformat(existing["last_analyzed"])
                if (datetime.utcnow() - last).total_seconds() < 21600:
                    print(f"[Scanner] Skip {symbol} (analyzed <6h ago)")
                    continue
            except Exception:
                pass

        windows = detect_pump_dump_windows(addr, symbol)

        store.upsert_token(addr, {
            **token,
            "flagged":      len(windows) > 0,
            "pump_windows": len(windows),
            "cex_listings": token.get("cex_listings", []),
            "on_bitget":    token.get("on_bitget", False),
        })

        if windows:
            for w in windows:
                store.add_pump_event(w)
            flagged.append(addr)
            total_events += len(windows)
            cexs = ", ".join(token.get("cex_listings", [])[:3])
            print(f"[Scanner] 🚨 {symbol:10s} — {len(windows)} window(s) | CEX: {cexs}")
        else:
            print(f"[Scanner] ✓  {symbol:10s} — no pump pattern")

        time.sleep(0.4)

    store.log_scan({
        "candidates_checked": len(raw),
        "cex_passed":         len(cex_listed),
        "tokens_flagged":     len(flagged),
        "pump_events_found":  total_events,
    })
    store.save()

    print(f"[Scanner] ══ Done: {len(flagged)} flagged from {len(cex_listed)} CEX-listed ══\n")
    return flagged
