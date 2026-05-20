"""
scanner/token_scanner.py  (v5 — zero CoinGecko dependency)
────────────────────────────────────────────────────────────
All sources are rate-limit free:
  A. DexScreener /token-boosts/top      — boosted tokens
  B. DexScreener /token-boosts/latest   — recently boosted
  C. DexScreener search (4 queries)     — broad ETH sweep
  D. DeFiLlama /protocols               — TVL-ranked ETH protocols
  E. Uniswap v3 subgraph                — top volume pools
  F. CoinGecko trending                 — ONE call only, cached

CoinGecko is used for ONE call (trending) with a long retry.
Everything else is DexScreener or DeFiLlama.
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

DS_BOOSTS_TOP    = "https://api.dexscreener.com/token-boosts/top/v1"
DS_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DS_SEARCH        = "https://api.dexscreener.com/latest/dex/search?q="
DS_TOKEN_PAIRS   = "https://api.dexscreener.com/latest/dex/tokens/"
DEFILLAMA_PROTOCOLS = "https://api.llama.fi/protocols"
UNISWAP_SUBGRAPH = "https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3"
CG_TRENDING      = "https://api.coingecko.com/api/v3/search/trending"

UNISWAP_QUERY = """{
  pools(first: 30, orderBy: volumeUSD, orderDirection: desc,
        where: {volumeUSD_gt: "500000"}) {
    token0 { id symbol }
    token1 { id symbol }
  }
}"""


def _get(url: str, timeout: int = 12, retries: int = 1) -> dict | list | None:
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"User-Agent": "sentinel/2.0"})
            if r.status_code == 429:
                if attempt < retries:
                    wait = 20 * (attempt + 1)
                    print(f"[Scanner] 429 on {url[:50]}... retry in {wait}s")
                    time.sleep(wait)
                    continue
                return None
            if r.ok:
                return r.json()
            if r.status_code != 404:
                print(f"[Scanner] HTTP {r.status_code}: {url[:65]}")
            return None
        except Exception as e:
            print(f"[Scanner] Error {url[:50]}: {e}")
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
        print(f"[Scanner] POST error {url[:50]}: {e}")
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
                        "source":  f"ds_boost_{label}",
                    })
        time.sleep(0.3)
    print(f"[Scanner] DexScreener boosts: {len(results)}")
    return results


# ── Source C: DexScreener search ──────────────────────────────────────────────

def _from_dexscreener_search() -> list[dict]:
    results = []
    queries = ["ethereum", "eth defi", "erc20", "ethereum meme"]
    for q in queries:
        data = _get(DS_SEARCH + q.replace(" ", "%20"))
        if data:
            for p in (data.get("pairs") or [])[:15]:
                if p.get("chainId") != "ethereum":
                    continue
                base = p.get("baseToken") or {}
                addr = base.get("address", "").lower()
                sym  = base.get("symbol", "?")
                vol  = float((p.get("volume") or {}).get("h24", 0) or 0)
                if addr and vol > 10000:
                    results.append({"address": addr, "symbol": sym,
                                    "source": "ds_search"})
        time.sleep(0.3)
    # Deduplicate within this source
    seen, unique = set(), []
    for r in results:
        if r["address"] not in seen:
            seen.add(r["address"])
            unique.append(r)
    print(f"[Scanner] DexScreener search: {len(unique)}")
    return unique


# ── Source D: DeFiLlama ───────────────────────────────────────────────────────

def _from_defillama() -> list[dict]:
    results = []
    data = _get(DEFILLAMA_PROTOCOLS, timeout=20)
    if not data or not isinstance(data, list):
        print("[Scanner] DeFiLlama: no data")
        return []

    eth_protocols = [
        p for p in data
        if p.get("chain") == "Ethereum"
        and float(p.get("tvl", 0) or 0) > 500_000
    ]
    eth_protocols.sort(
        key=lambda p: abs(float(p.get("change_1h", 0) or 0)),
        reverse=True
    )

    for p in eth_protocols[:40]:
        address = (p.get("address") or "").lower()
        symbol  = p.get("symbol", "?")
        if address and address.startswith("0x") and len(address) == 42:
            results.append({
                "address": address,
                "symbol":  symbol,
                "source":  "defillama",
            })

    print(f"[Scanner] DeFiLlama: {len(results)}")
    return results


# ── Source E: Uniswap v3 subgraph ─────────────────────────────────────────────

def _from_uniswap() -> list[dict]:
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
                    results.append({"address": addr, "symbol": sym,
                                    "source": "uniswap_v3"})
    print(f"[Scanner] Uniswap v3: {len(results)}")
    return results


# ── Source F: CoinGecko trending (one call, long retry) ───────────────────────

def _from_coingecko_trending() -> list[dict]:
    results = []
    # Try once with a long timeout — if 429, skip entirely this cycle
    try:
        r = requests.get(CG_TRENDING, timeout=15,
                         headers={"User-Agent": "sentinel/2.0"})
        if r.status_code == 429:
            print("[Scanner] CoinGecko trending: rate limited, skipping")
            return []
        if r.ok:
            data = r.json()
            for coin in data.get("coins", [])[:20]:
                item      = coin.get("item", {})
                platforms = item.get("platforms", {})
                eth_addr  = platforms.get("ethereum", "").lower()
                if eth_addr:
                    results.append({
                        "address": eth_addr,
                        "symbol":  item.get("symbol", "?").upper(),
                        "source":  "cg_trending",
                    })
    except Exception as e:
        print(f"[Scanner] CoinGecko trending error: {e}")
    print(f"[Scanner] CoinGecko trending: {len(results)}")
    return results


# ── Pump/dump detection ────────────────────────────────────────────────────────

def detect_pump_dump_windows(token_address: str, symbol: str) -> list[dict]:
    data = _get(DS_TOKEN_PAIRS + token_address)
    if not data:
        return []
    pairs     = data.get("pairs") or []
    eth_pairs = [p for p in pairs if p.get("chainId") == "ethereum"]
    if not eth_pairs:
        return []
    pair    = max(eth_pairs,
                  key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
    changes = pair.get("priceChange") or {}
    volume  = pair.get("volume") or {}
    liq     = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    h1      = float(changes.get("h1",  0) or 0)
    h6      = float(changes.get("h6",  0) or 0)
    h24     = float(changes.get("h24", 0) or 0)
    d7      = float(changes.get("d7",  0) or 0) if "d7" in changes else None
    now     = datetime.utcnow()
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

    if (d7 is not None and abs(d7) < 8
            and (abs(h24) >= MIN_PRICE_CHANGE_PCT or abs(h6) >= MIN_PRICE_CHANGE_PCT)):
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
    all_candidates = []
    all_candidates += _from_dexscreener_boosts()
    all_candidates += _from_dexscreener_search()
    all_candidates += _from_defillama()
    all_candidates += _from_uniswap()
    all_candidates += _from_coingecko_trending()   # one call, skip on 429

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
    print(f"\n[Scanner] ══ Scan started "
          f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ══")
    sys.stdout.flush()

    raw = fetch_raw_candidates()
    if not raw:
        print("[Scanner] No candidates found")
        store.log_scan({"candidates_checked": 0, "cex_passed": 0,
                        "tokens_flagged": 0, "pump_events_found": 0})
        return []

    cex_listed = filter_tokens(raw[:MAX_TOKENS_TO_SCAN * 3])
    if not cex_listed:
        print("[Scanner] No tokens passed CEX filter")
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
            reason = token.get("detection_reason", "")
            print(f"[Scanner] FLAGGED {symbol:12s} "
                  f"{len(windows)} window(s) | {reason}")
        else:
            print(f"[Scanner] clean   {symbol}")

        time.sleep(0.3)
        sys.stdout.flush()

    store.log_scan({
        "candidates_checked": len(raw),
        "cex_passed":         len(cex_listed),
        "tokens_flagged":     len(flagged),
        "pump_events_found":  total_events,
    })
    store.save()

    print(f"[Scanner] ══ Done: {len(flagged)}/{len(cex_listed)} flagged ══\n")
    sys.stdout.flush()
    return flagged
