"""
scanner/token_scanner.py  (v4 — rate-limit resilient)
───────────────────────────────────────────────────────
Sources used (all free, no key needed):
  A. DexScreener /token-boosts/top      — boosted tokens
  B. DexScreener /token-boosts/latest   — recently boosted
  C. DexScreener search queries          — broad ETH sweep
  D. DexScreener /tokens/ethereum/...   — top ETH pairs (fixed endpoint)
  E. CoinGecko trending                  — with 429 retry
  F. CoinGecko markets (ETH ecosystem)   — with 429 retry
  G. DeFiLlama /protocols               — TVL-ranked protocols (no rate limit)
  H. Uniswap subgraph (free, no key)    — top volume pools

All requests have retry-on-429 with backoff. Any single source
failing never stops the rest.
"""

import os
import sys
import time
import requests
from datetime import datetime, timedelta
from data.store import store
from scanner.cex_filter import filter_tokens

MIN_PRICE_CHANGE_PCT = float(os.getenv("MIN_PRICE_CHANGE_PCT", 15))
MAX_TOKENS_TO_SCAN   = int(os.getenv("MAX_TOKENS_TO_SCAN",    20))

# ── DexScreener endpoints (all working as of 2025) ────────────────────────────
DS_BOOSTS_TOP    = "https://api.dexscreener.com/token-boosts/top/v1"
DS_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DS_SEARCH        = "https://api.dexscreener.com/latest/dex/search?q="
DS_TOKEN_PAIRS   = "https://api.dexscreener.com/latest/dex/tokens/"

# ── CoinGecko (free tier — has 429s, use with retry) ─────────────────────────
CG_TRENDING   = "https://api.coingecko.com/api/v3/search/trending"
CG_MARKETS    = ("https://api.coingecko.com/api/v3/coins/markets"
                 "?vs_currency=usd&category=ethereum-ecosystem"
                 "&order=volume_desc&per_page=50&page=1"
                 "&price_change_percentage=1h,24h")

# ── DeFiLlama (no rate limits, very reliable) ─────────────────────────────────
DEFILLAMA_PROTOCOLS = "https://api.llama.fi/protocols"

# ── Uniswap v3 subgraph (free, no key needed) ─────────────────────────────────
UNISWAP_SUBGRAPH = "https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3"
UNISWAP_QUERY = """
{
  pools(first: 30, orderBy: volumeUSD, orderDirection: desc,
        where: {volumeUSD_gt: "100000"}) {
    token0 { id symbol }
    token1 { id symbol }
    volumeUSD
  }
}
"""


def _get(url: str, timeout: int = 12, retries: int = 2) -> dict | list | None:
    """GET with automatic retry on 429."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "sentinel/2.0"})
            if r.status_code == 429:
                wait = 12 * (attempt + 1)
                print(f"[Scanner] 429 on {url[:55]}... waiting {wait}s")
                time.sleep(wait)
                continue
            if r.ok:
                return r.json()
            print(f"[Scanner] HTTP {r.status_code}: {url[:70]}")
            return None
        except Exception as e:
            print(f"[Scanner] Error {url[:55]}: {e}")
            if attempt < retries:
                time.sleep(3)
    return None


def _post(url: str, payload: dict, timeout: int = 12) -> dict | None:
    try:
        r = requests.post(url, json=payload, timeout=timeout,
                          headers={"User-Agent": "sentinel/2.0"})
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"[Scanner] POST error {url[:55]}: {e}")
    return None


# ── Source A+B: DexScreener boosts ────────────────────────────────────────────

def _from_dexscreener_boosts() -> list[dict]:
    results = []
    for label, url in [("top", DS_BOOSTS_TOP), ("latest", DS_BOOSTS_LATEST)]:
        data = _get(url)
        if data and isinstance(data, list):
            for item in data:
                addr  = item.get("tokenAddress", "")
                chain = item.get("chainId", "")
                if addr and chain in ("ethereum", "eth"):
                    results.append({
                        "address": addr.lower(),
                        "symbol":  item.get("description", "?")[:20],
                        "source":  f"dexscreener_boost_{label}",
                    })
        time.sleep(0.5)
    print(f"[Scanner] DexScreener boosts: {len(results)} ETH tokens")
    return results


# ── Source C: DexScreener search ──────────────────────────────────────────────

def _from_dexscreener_search() -> list[dict]:
    results = []
    queries = ["ethereum", "eth token", "ethereum defi", "erc20"]
    for q in queries:
        data = _get(DS_SEARCH + q.replace(" ", "%20"))
        if data:
            for p in (data.get("pairs") or [])[:10]:
                if p.get("chainId") != "ethereum":
                    continue
                base = p.get("baseToken") or {}
                addr = base.get("address", "").lower()
                sym  = base.get("symbol", "?")
                # Only tokens with real volume
                vol = float((p.get("volume") or {}).get("h24", 0) or 0)
                if addr and vol > 50000:
                    results.append({"address": addr, "symbol": sym,
                                    "source": "dexscreener_search", "volume_24h": vol})
        time.sleep(0.5)
    print(f"[Scanner] DexScreener search: {len(results)} ETH tokens")
    return results


# ── Source D: DexScreener token pairs for known high-volume tokens ─────────────

def _from_dexscreener_token_lookup() -> list[dict]:
    """
    Look up pairs for a small set of known active Ethereum tokens.
    This gives us fresh price change data without hitting broken endpoints.
    """
    # Well-known CEX-listed ETH tokens — used as seeds
    seed_addresses = [
        "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
        "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",  # UNI
        "0x514910771af9ca656af840dff83e8264ecf986ca",  # LINK
        "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9",  # AAVE
        "0xd533a949740bb3306d119cc777fa900ba034cd52",  # CRV
        "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2",  # MKR
        "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72",  # ENS
        "0x3506424f91fd33084466f402d5d97f05f8e3b4af",  # CHZ
        "0x0f5d2fb29fb7d3cfee444a200298f468908cc942",  # MANA
        "0xbb0e17ef65f82ab018d8edd776e8dd940327b28b",  # AXS
    ]
    results = []
    for addr in seed_addresses[:5]:  # only check 5 to stay fast
        data = _get(DS_TOKEN_PAIRS + addr)
        if data:
            pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "ethereum"]
            if pairs:
                best = max(pairs, key=lambda p: float((p.get("volume") or {}).get("h24", 0) or 0))
                base = best.get("baseToken") or {}
                results.append({
                    "address": addr.lower(),
                    "symbol":  base.get("symbol", "?"),
                    "source":  "dexscreener_seed",
                })
        time.sleep(0.3)
    print(f"[Scanner] DexScreener seed lookup: {len(results)} tokens")
    return results


# ── Source E: CoinGecko trending (with retry) ─────────────────────────────────

def _from_coingecko_trending() -> list[dict]:
    results = []
    data = _get(CG_TRENDING, retries=3)
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


# ── Source F: CoinGecko markets (with retry + longer wait) ────────────────────

def _from_coingecko_markets() -> list[dict]:
    results = []
    # Wait a bit before hitting CoinGecko again after trending call
    time.sleep(8)
    data = _get(CG_MARKETS, retries=3, timeout=20)
    if data and isinstance(data, list):
        for coin in data:
            # CoinGecko markets doesn't return ETH address inline
            # Use symbol + coin_id for CEX filter to resolve
            change_1h  = coin.get("price_change_percentage_1h_in_currency") or 0
            change_24h = coin.get("price_change_percentage_24h") or 0
            if abs(change_1h) >= MIN_PRICE_CHANGE_PCT or abs(change_24h) >= MIN_PRICE_CHANGE_PCT:
                # Store coin_id so CEX filter can resolve address
                results.append({
                    "address":    "",  # resolved by CEX filter via coin_id
                    "coin_id":    coin.get("id", ""),
                    "symbol":     (coin.get("symbol") or "?").upper(),
                    "change_1h":  change_1h,
                    "change_24h": change_24h,
                    "source":     "coingecko_markets",
                })
    # Filter out entries without addresses for now
    with_addr = [r for r in results if r.get("address")]
    print(f"[Scanner] CoinGecko markets (moving): {len(with_addr)} ETH tokens")
    return with_addr


# ── Source G: DeFiLlama protocols (no rate limit) ─────────────────────────────

def _from_defillama() -> list[dict]:
    """
    DeFiLlama has no rate limits and lists all major DeFi protocols
    with their token addresses. Great stable source.
    """
    results = []
    data = _get(DEFILLAMA_PROTOCOLS, timeout=20)
    if not data or not isinstance(data, list):
        print("[Scanner] DeFiLlama: no data")
        return []

    # Filter to Ethereum protocols with real TVL
    eth_protocols = [
        p for p in data
        if p.get("chain") == "Ethereum"
        and float(p.get("tvl", 0) or 0) > 1_000_000
    ]

    # Sort by TVL change (biggest movers = pump candidates)
    eth_protocols.sort(key=lambda p: abs(float(p.get("change_1h", 0) or 0)), reverse=True)

    for p in eth_protocols[:30]:
        address = (p.get("address") or "").lower()
        symbol  = p.get("symbol", "?")
        change  = abs(float(p.get("change_1h", 0) or 0))

        if address and address.startswith("0x") and len(address) == 42:
            results.append({
                "address": address,
                "symbol":  symbol,
                "source":  "defillama",
                "tvl_change_1h": change,
            })

    print(f"[Scanner] DeFiLlama: {len(results)} ETH tokens")
    return results


# ── Source H: Uniswap v3 subgraph ─────────────────────────────────────────────

def _from_uniswap_subgraph() -> list[dict]:
    results = []
    data = _post(UNISWAP_SUBGRAPH, {"query": UNISWAP_QUERY})
    if data:
        pools = (data.get("data") or {}).get("pools") or []
        seen  = set()
        for pool in pools:
            for key in ["token0", "token1"]:
                token = pool.get(key) or {}
                addr  = token.get("id", "").lower()
                sym   = token.get("symbol", "?")
                if addr and addr not in seen:
                    seen.add(addr)
                    results.append({
                        "address": addr,
                        "symbol":  sym,
                        "source":  "uniswap_v3",
                    })
    print(f"[Scanner] Uniswap v3 subgraph: {len(results)} tokens")
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


def detect_pump_dump_windows(token_address: str, symbol: str) -> list[dict]:
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
    now = datetime.utcnow()
    windows = []

    if h1 >= MIN_PRICE_CHANGE_PCT and h24 < (h1 * 0.5):
        windows.append({"token": token_address.lower(), "symbol": symbol,
                        "type": "short_pump_dump", "pump_pct": round(h1, 2),
                        "dump_pct": round(h1 - h24, 2),
                        "window_start": (now - timedelta(hours=25)).isoformat(),
                        "window_end": now.isoformat(), "liquidity_usd": liq,
                        "volume_h24": float(volume.get("h24", 0) or 0)})

    if h6 >= MIN_PRICE_CHANGE_PCT and h24 < (h6 * 0.5):
        windows.append({"token": token_address.lower(), "symbol": symbol,
                        "type": "mid_pump_dump", "pump_pct": round(h6, 2),
                        "dump_pct": round(h6 - h24, 2),
                        "window_start": (now - timedelta(hours=25)).isoformat(),
                        "window_end": now.isoformat(), "liquidity_usd": liq,
                        "volume_h24": float(volume.get("h24", 0) or 0)})

    if d7 is not None and abs(d7) < 8 and (abs(h24) >= MIN_PRICE_CHANGE_PCT or abs(h6) >= MIN_PRICE_CHANGE_PCT):
        windows.append({"token": token_address.lower(), "symbol": symbol,
                        "type": "repeated_cycle",
                        "pump_pct": round(max(abs(h24), abs(h6)), 2),
                        "dump_pct": round(abs(d7), 2),
                        "window_start": (now - timedelta(days=8)).isoformat(),
                        "window_end": now.isoformat(), "liquidity_usd": liq,
                        "volume_h24": float(volume.get("h24", 0) or 0)})

    return windows


# ── Main scan ──────────────────────────────────────────────────────────────────

def fetch_raw_candidates() -> list[dict]:
    """Run all sources, collect, deduplicate."""
    all_candidates = []

    # Fast sources first (DexScreener has no rate limits)
    all_candidates += _from_dexscreener_boosts()
    all_candidates += _from_dexscreener_search()
    all_candidates += _from_dexscreener_token_lookup()
    all_candidates += _from_defillama()
    all_candidates += _from_uniswap_subgraph()

    # Slower sources (CoinGecko — rate limited, but retry handles it)
    all_candidates += _from_coingecko_trending()
    all_candidates += _from_coingecko_markets()

    # Deduplicate by address
    seen, unique = set(), []
    for c in all_candidates:
        addr = c.get("address", "").lower()
        if addr and len(addr) == 42 and addr.startswith("0x") and addr not in seen:
            seen.add(addr)
            unique.append(c)

    print(f"[Scanner] Total unique ETH candidates: {len(unique)}")
    sys.stdout.flush()
    return unique


def run_scan() -> list[str]:
    print(f"\n[Scanner] ══ Scan started {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ══")
    sys.stdout.flush()

    raw = fetch_raw_candidates()
    if not raw:
        print("[Scanner] No candidates found — all sources returned empty")
        store.log_scan({"candidates_checked": 0, "cex_passed": 0,
                        "tokens_flagged": 0, "pump_events_found": 0})
        return []

    cex_listed = filter_tokens(raw[:MAX_TOKENS_TO_SCAN * 3])
    if not cex_listed:
        print("[Scanner] No CEX-listed tokens passed filter")
        store.log_scan({"candidates_checked": len(raw), "cex_passed": 0,
                        "tokens_flagged": 0, "pump_events_found": 0})
        return []

    cex_listed   = cex_listed[:MAX_TOKENS_TO_SCAN]
    flagged      = []
    total_events = 0

    for token in cex_listed:
        addr   = token["address"]
        symbol = token.get("symbol", "?")

        existing = store.get_token(addr)
        if existing.get("last_analyzed"):
            try:
                last = datetime.fromisoformat(existing["last_analyzed"])
                if (datetime.utcnow() - last).total_seconds() < 21600:
                    print(f"[Scanner] Skip {symbol} (<6h)")
                    continue
            except Exception:
                pass

        windows = detect_pump_dump_windows(addr, symbol)
        store.upsert_token(addr, {**token, "flagged": len(windows) > 0,
                                   "pump_windows": len(windows)})

        if windows:
            for w in windows:
                store.add_pump_event(w)
            flagged.append(addr)
            total_events += len(windows)
            cexs = ", ".join(token.get("cex_listings", [])[:3])
            print(f"[Scanner] FLAGGED {symbol:12s} — {len(windows)} window(s) | {cexs}")
        else:
            print(f"[Scanner] clean   {symbol}")

        time.sleep(0.3)
        sys.stdout.flush()

    store.log_scan({"candidates_checked": len(raw), "cex_passed": len(cex_listed),
                    "tokens_flagged": len(flagged), "pump_events_found": total_events})
    store.save()

    print(f"[Scanner] ══ Done: {len(flagged)}/{len(cex_listed)} flagged ══\n")
    sys.stdout.flush()
    return flagged
