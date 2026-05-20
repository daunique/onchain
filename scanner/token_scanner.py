"""
scanner/token_scanner.py  (v8 — ETH + Base, sub-$20M market cap focus)

TARGET PROFILE:
  - Market cap: under $20M  (small cap, highly manipulable)
  - Liquidity:  $100K – $8M (thin enough to move, real enough to trade)
  - Volume:     $50K+ 24h
  - Chains:     Ethereum mainnet + Base

  Why sub-$20M? Tokens in this band need far less capital to manipulate.
  A coordinated group can move price 20-40% with $200K-$500K. Mega-caps
  require tens of millions and are not the target.

  Market cap source priority (no CoinGecko needed):
    1. DexScreener `marketCap` field  — circulating supply × price
    2. DexScreener `fdv` field        — fully diluted (fallback)
    3. DeFiLlama   `mcap` field       — on protocol objects
    If all three are 0/null the token passes through; liquidity cap
    ($8M MAX_LIQUIDITY) acts as a soft proxy ceiling.

PATTERNS DETECTED:
  P3 - PUMP_DUMP_CYCLE:     spike up then full retracement
  P4 - SAWTOOTH:            oscillates in band, never breaks out
  P5 - LOW_FLOAT_SPIKE:     tiny liquidity + outsized % move
  P6 - REVERSAL_AFTER_PUMP: pumped h1/h6, now dumping in h24
  P7 - UNUSUAL_VOLUME:      vol >> liquidity (wash trading)
  P8 - STRONG_MOVE_MONITOR: big move worth downloading transfer history

CHAIN IDs (DexScreener):
  ethereum  — Ethereum mainnet
  base      — Base (Coinbase L2)
"""

import os
import sys
import time
import random
import requests
from datetime import datetime, timedelta
from data.store import store
from scanner.cex_filter import filter_tokens

# ── Thresholds ─────────────────────────────────────────────────────────────────
MIN_PUMP_PCT       = float(os.getenv("MIN_PRICE_CHANGE_PCT",  8))
MAX_TOKENS_TO_SCAN = int(os.getenv("MAX_TOKENS_TO_SCAN",     50))   # raised for 2 chains
MAX_MARKET_CAP_USD = int(os.getenv("MAX_MARKET_CAP_USD", 20_000_000))   # $20M hard ceiling
MIN_LIQUIDITY      = 100_000      # $100K
MAX_LIQUIDITY      = 8_000_000    # $8M
UNUSUAL_VOL_RATIO  = 3.0

# ── Supported chains ───────────────────────────────────────────────────────────
# DexScreener chain IDs we care about
TARGET_CHAINS = {"ethereum", "base"}

# ── Endpoints ──────────────────────────────────────────────────────────────────
DS_BOOSTS_TOP    = "https://api.dexscreener.com/token-boosts/top/v1"
DS_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DS_SEARCH        = "https://api.dexscreener.com/latest/dex/search?q="
DS_TOKEN_PAIRS   = "https://api.dexscreener.com/latest/dex/tokens/"
DEFILLAMA_ALL    = "https://api.llama.fi/protocols"
CG_TRENDING      = "https://api.coingecko.com/api/v3/search/trending"

SEARCH_QUERIES = [
    "pepe", "doge", "shib", "floki", "wojak",
    "ai", "gpt", "agi", "turbo", "based",
    "trump", "maga", "brett", "andy", "toshi",
    "mew", "neiro", "ponke", "michi", "cat",
    "inu", "elon", "moon", "baby", "safe",
    "chad", "sigma", "monk", "frog", "bonk",
    "myro", "popcat", "wif", "pengu", "mog",
    "pnut", "goat", "act", "npc", "turbo",
]


# ── Market cap helper ──────────────────────────────────────────────────────────

def _cap_from_pair(p: dict) -> float:
    """
    Extract best market cap estimate from a DexScreener pair object.
    Prefers circulating marketCap; falls back to fdv.
    Returns 0 if neither is available (caller should treat as unknown).
    """
    mcap = float(p.get("marketCap") or 0)
    fdv  = float(p.get("fdv")       or 0)
    return mcap if mcap > 0 else fdv


def _cap_ok(cap: float) -> bool:
    """
    True if the token passes the market cap filter.
    Tokens with cap == 0 (unknown supply data) are allowed through —
    the liquidity ceiling acts as a soft proxy in that case.
    """
    if cap <= 0:
        return True   # unknown — let liquidity ceiling handle it
    return cap <= MAX_MARKET_CAP_USD


# ── HTTP helper ────────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 15) -> dict | list | None:
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "sentinel/3.0"})
        if r.status_code == 429:
            print(f"[Scanner] 429 skipped: {url[:50]}")
            return None
        if r.ok:
            try:
                return r.json()
            except Exception:
                text = r.text
                for end in (len(text), len(text) - 100, len(text) - 500):
                    try:
                        import json
                        return json.loads(text[:end].rsplit(",", 1)[0] + "]")
                    except Exception:
                        pass
                print(f"[Scanner] JSON parse failed: {url[:50]}")
                return None
        if r.status_code != 404:
            print(f"[Scanner] HTTP {r.status_code}: {url[:60]}")
    except Exception as e:
        print(f"[Scanner] Error: {e}")
    return None


# ── Source A: DexScreener boosts (ETH + Base) ─────────────────────────────────

def _from_dexscreener_boosts() -> list[dict]:
    results = []
    for url in [DS_BOOSTS_TOP, DS_BOOSTS_LATEST]:
        data = _get(url)
        if data and isinstance(data, list):
            for item in data:
                addr  = item.get("tokenAddress", "")
                chain = item.get("chainId", "")
                if addr and chain in TARGET_CHAINS:
                    results.append({
                        "address": addr.lower(),
                        "symbol":  item.get("description", "?")[:20],
                        "chain":   chain,
                        "source":  "ds_boost",
                    })
        time.sleep(0.3)
    print(f"[Scanner] DS boosts: {len(results)} (ETH+Base)")
    return results


# ── Source B: DexScreener rotating search (ETH + Base) ────────────────────────

def _from_dexscreener_search() -> list[dict]:
    results = []
    queries = random.sample(SEARCH_QUERIES, min(10, len(SEARCH_QUERIES)))
    for q in queries:
        data = _get(DS_SEARCH + q.replace(" ", "%20"))
        if data:
            for p in (data.get("pairs") or [])[:40]:
                chain = p.get("chainId", "")
                if chain not in TARGET_CHAINS:
                    continue
                base    = p.get("baseToken") or {}
                addr    = base.get("address", "").lower()
                sym     = base.get("symbol", "?")
                liq     = float((p.get("liquidity") or {}).get("usd", 0) or 0)
                vol     = float((p.get("volume")    or {}).get("h24", 0) or 0)
                changes = p.get("priceChange") or {}
                cap     = _cap_from_pair(p)

                if not addr:
                    continue
                if not (MIN_LIQUIDITY <= liq <= MAX_LIQUIDITY):
                    continue
                if not _cap_ok(cap):
                    cap_m = cap / 1e6
                    print(f"[Scanner] SKIP {sym:10s} cap=${cap_m:.1f}M > ${MAX_MARKET_CAP_USD/1e6:.0f}M ceiling")
                    continue

                results.append({
                    "address": addr, "symbol": sym,
                    "chain":   chain, "source": "ds_search",
                    "liq": liq, "vol_24h": vol,
                    "market_cap": cap,
                    "h1":  float(changes.get("h1",  0) or 0),
                    "h6":  float(changes.get("h6",  0) or 0),
                    "h24": float(changes.get("h24", 0) or 0),
                    "d7":  float(changes.get("d7",  0) or 0) if "d7" in changes else None,
                })
        time.sleep(0.3)

    seen, unique = set(), []
    for r in results:
        key = f"{r['chain']}:{r['address']}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    print(f"[Scanner] DS search: {len(unique)} (ETH+Base, <${MAX_MARKET_CAP_USD/1e6:.0f}M cap)")
    return unique


# ── Source C: DeFiLlama mid-cap protocols (ETH + Base) ────────────────────────

def _from_defillama_midcap() -> list[dict]:
    """
    DeFiLlama protocols filtered to our target chains and cap range.
    Uses the `mcap` field when available; falls back to tvl as proxy.
    """
    results = []
    data = _get(DEFILLAMA_ALL, timeout=30)
    if not data or not isinstance(data, list):
        data = _get("https://api.llama.fi/v2/protocols", timeout=20)
    if not data or not isinstance(data, list):
        print("[Scanner] DeFiLlama: no data")
        return []

    # DeFiLlama chain names for our targets
    llama_chains = {"Ethereum", "Base"}

    eligible = []
    for p in data:
        chain = p.get("chain", "")
        if chain not in llama_chains:
            continue

        tvl  = float(p.get("tvl",  0) or 0)
        mcap = float(p.get("mcap", 0) or 0)
        fdv  = float(p.get("fdv",  0) or 0)

        # Use best cap estimate available from DeFiLlama
        cap = mcap if mcap > 0 else fdv

        # TVL range proxy (still useful even when cap is known)
        if not (50_000 < tvl < 50_000_000):
            continue

        # Apply cap filter — skip knowns above ceiling
        if not _cap_ok(cap):
            continue

        eligible.append(p)

    # Sort by absolute 1h TVL change — biggest movers first
    eligible.sort(key=lambda p: abs(float(p.get("change_1h", 0) or 0)), reverse=True)

    for p in eligible[:60]:
        address = (p.get("address") or "").lower()
        symbol  = p.get("symbol", "?")
        change  = float(p.get("change_1h", 0) or 0)
        chain   = p.get("chain", "Ethereum")
        cap     = float(p.get("mcap", 0) or p.get("fdv", 0) or 0)

        # Map DeFiLlama chain name → our internal chain id
        chain_id = "base" if chain == "Base" else "ethereum"

        if address and address.startswith("0x") and len(address) == 42:
            results.append({
                "address":      address,
                "symbol":       symbol,
                "chain":        chain_id,
                "source":       "defillama_midcap",
                "market_cap":   cap,
                "defillama_1h": change,
            })

    print(f"[Scanner] DeFiLlama mid-cap: {len(results)} (ETH+Base)")
    return results


# ── Source D: CoinGecko trending (ETH + Base addresses, single call) ──────────

def _from_coingecko_trending() -> list[dict]:
    """
    Single CoinGecko call — no pagination, no per-coin lookups.
    Filters by market cap rank as a proxy (rank 500-5000 ~ $1M-$20M range).
    Base chain address extracted from platforms map.
    """
    results = []
    try:
        r = requests.get(CG_TRENDING, timeout=12,
                         headers={"User-Agent": "sentinel/3.0"})
        if r.status_code == 429:
            print("[Scanner] CG trending: 429, skipping")
            return []
        if r.ok:
            for coin in r.json().get("coins", [])[:20]:
                item      = coin.get("item", {})
                platforms = item.get("platforms") or {}
                rank      = item.get("market_cap_rank", 9999)

                # rank 500-5000 roughly corresponds to $1M–$20M range
                if not (500 <= rank <= 5000):
                    continue

                # Check ETH address
                eth_addr = platforms.get("ethereum", "").lower()
                if eth_addr:
                    results.append({
                        "address":    eth_addr,
                        "symbol":     item.get("symbol", "?").upper(),
                        "chain":      "ethereum",
                        "source":     "cg_trending",
                        "market_cap": 0,   # rank-filtered but no $ value here
                        "cg_rank":    rank,
                    })

                # Check Base address
                base_addr = platforms.get("base", "").lower()
                if base_addr:
                    results.append({
                        "address":    base_addr,
                        "symbol":     item.get("symbol", "?").upper(),
                        "chain":      "base",
                        "source":     "cg_trending",
                        "market_cap": 0,
                        "cg_rank":    rank,
                    })

    except Exception as e:
        print(f"[Scanner] CG trending error: {e}")
    print(f"[Scanner] CG trending: {len(results)} (ETH+Base, rank 500-5000)")
    return results


# ── Source E: Unusual volume scan (ETH + Base) ────────────────────────────────

def _from_unusual_volume_scan() -> list[dict]:
    """
    Tokens where 24h volume >> liquidity across both chains.
    vol/liq > 3x is a classic wash trading / coordinated pump signal.
    """
    results = []
    queries = ["ethereum volume", "base volume", "eth high volume"]
    for q in random.sample(queries, 2):
        data = _get(DS_SEARCH + q.replace(" ", "%20"))
        if data:
            for p in (data.get("pairs") or [])[:30]:
                chain = p.get("chainId", "")
                if chain not in TARGET_CHAINS:
                    continue
                base    = p.get("baseToken") or {}
                addr    = base.get("address", "").lower()
                sym     = base.get("symbol", "?")
                liq     = float((p.get("liquidity") or {}).get("usd", 0) or 0)
                vol     = float((p.get("volume")    or {}).get("h24", 0) or 0)
                changes = p.get("priceChange") or {}
                cap     = _cap_from_pair(p)

                if not addr or liq <= 0:
                    continue
                if not _cap_ok(cap):
                    continue

                vol_ratio = vol / liq
                if (MIN_LIQUIDITY <= liq <= MAX_LIQUIDITY
                        and vol_ratio >= UNUSUAL_VOL_RATIO):
                    results.append({
                        "address":   addr, "symbol": sym,
                        "chain":     chain, "source": "unusual_volume",
                        "liq":       liq, "vol_24h": vol,
                        "market_cap": cap,
                        "vol_ratio": round(vol_ratio, 1),
                        "h1":  float(changes.get("h1",  0) or 0),
                        "h6":  float(changes.get("h6",  0) or 0),
                        "h24": float(changes.get("h24", 0) or 0),
                        "d7":  float(changes.get("d7",  0) or 0) if "d7" in changes else None,
                    })
        time.sleep(0.3)

    seen, unique = set(), []
    for r in results:
        key = f"{r['chain']}:{r['address']}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    print(f"[Scanner] Unusual volume: {len(unique)} (ETH+Base)")
    return unique


# ── Pattern detection ──────────────────────────────────────────────────────────

def score_manipulation_patterns(token: dict) -> list[dict]:
    """
    Score a token against all manipulation patterns.
    Returns list of detected pattern windows (empty = clean).
    Chain-aware: fetches pairs filtered to token's chain.
    """
    addr   = token["address"]
    symbol = token.get("symbol", "?")
    chain  = token.get("chain", "ethereum")
    now    = datetime.utcnow()

    h1  = token.get("h1")
    h6  = token.get("h6")
    h24 = token.get("h24")
    d7  = token.get("d7")
    liq = token.get("liq", 0)
    vol = token.get("vol_24h", 0)
    cap = token.get("market_cap", 0)

    if h1 is None or h24 is None:
        data = _get(DS_TOKEN_PAIRS + addr)
        if not data:
            return []
        pairs       = data.get("pairs") or []
        chain_pairs = [p for p in pairs if p.get("chainId") == chain]
        if not chain_pairs:
            # Fallback: accept any supported chain pair if specific chain missing
            chain_pairs = [p for p in pairs if p.get("chainId") in TARGET_CHAINS]
        if not chain_pairs:
            return []
        best    = max(chain_pairs,
                      key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
        changes = best.get("priceChange") or {}
        liq     = float((best.get("liquidity") or {}).get("usd", 0) or 0)
        vol     = float((best.get("volume")    or {}).get("h24", 0) or 0)
        cap     = _cap_from_pair(best)
        h1      = float(changes.get("h1",  0) or 0)
        h6      = float(changes.get("h6",  0) or 0)
        h24     = float(changes.get("h24", 0) or 0)
        d7      = float(changes.get("d7",  0) or 0) if "d7" in changes else None

    # Hard reject: outside liquidity range
    if liq < MIN_LIQUIDITY or liq > MAX_LIQUIDITY:
        return []

    # Hard reject: known cap above ceiling
    if not _cap_ok(cap):
        return []

    MIN       = MIN_PUMP_PCT
    windows   = []
    vol_ratio = vol / liq if liq > 0 else 0

    base = {
        "token":        addr,
        "symbol":       symbol,
        "chain":        chain,
        "window_start": (now - timedelta(hours=25)).isoformat(),
        "window_end":   now.isoformat(),
        "liquidity_usd": liq,
        "volume_h24":   vol,
        "market_cap":   cap,
    }

    # P3: Classic pump then dump
    if h1 >= MIN and h24 < (h1 * 0.5):
        windows.append({**base, "type": "P3_pump_dump_h1",
                        "pump_pct": round(h1, 2),
                        "dump_pct": round(h1 - h24, 2)})

    if h6 >= MIN and h24 < (h6 * 0.5):
        windows.append({**base, "type": "P3_pump_dump_h6",
                        "pump_pct": round(h6, 2),
                        "dump_pct": round(h6 - h24, 2)})

    # P4: Sawtooth — flat week but big intraday swings
    if (d7 is not None
            and abs(d7) < 12
            and abs(h24) >= MIN
            and abs(h1) >= MIN * 0.5):
        windows.append({**base, "type": "P4_sawtooth_cycle",
                        "pump_pct": round(max(abs(h24), abs(h6 or 0)), 2),
                        "dump_pct": round(abs(d7), 2)})

    # P5: Low float spike
    if liq < 1_000_000 and abs(h1) >= MIN * 1.5:
        windows.append({**base, "type": "P5_low_float_spike",
                        "pump_pct": round(abs(h1), 2), "dump_pct": 0})

    # P6: Reversal in progress
    if h6 >= MIN and h24 < 0 and (h6 - h24) >= MIN * 1.5:
        windows.append({**base, "type": "P6_reversal_in_progress",
                        "pump_pct": round(h6, 2),
                        "dump_pct": round(h6 - h24, 2)})

    # P7: Wash trading / unusual volume
    if vol_ratio >= UNUSUAL_VOL_RATIO and abs(h24) >= MIN * 0.5:
        windows.append({**base, "type": "P7_unusual_volume_ratio",
                        "pump_pct": round(abs(h24), 2),
                        "dump_pct": round(vol_ratio, 1)})

    # P8: Strong move — worth monitoring
    if (abs(h24) >= MIN * 2 or abs(h6) >= MIN * 2) and not windows:
        windows.append({**base, "type": "P8_strong_move_monitor",
                        "pump_pct": round(max(abs(h24), abs(h6 or 0)), 2),
                        "dump_pct": 0})

    return windows


# ── Main scan ──────────────────────────────────────────────────────────────────

def fetch_raw_candidates() -> list[dict]:
    all_candidates = []
    all_candidates += _from_dexscreener_boosts()
    all_candidates += _from_dexscreener_search()
    all_candidates += _from_defillama_midcap()
    all_candidates += _from_unusual_volume_scan()
    all_candidates += _from_coingecko_trending()

    # Dedup by chain:address (same token on ETH and Base are different assets)
    seen, unique = set(), []
    for c in all_candidates:
        chain = c.get("chain", "ethereum")
        addr  = c.get("address", "").lower()
        if addr and len(addr) == 42 and addr.startswith("0x"):
            key = f"{chain}:{addr}"
            if key not in seen:
                seen.add(key)
                unique.append(c)

    eth_count  = sum(1 for c in unique if c.get("chain") == "ethereum")
    base_count = sum(1 for c in unique if c.get("chain") == "base")
    print(f"[Scanner] Total unique candidates: {len(unique)} "
          f"(ETH={eth_count}, Base={base_count})")
    sys.stdout.flush()
    return unique


def run_scan() -> list[str]:
    print(f"\n[Scanner] ══ Scan {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} "
          f"| cap<${MAX_MARKET_CAP_USD/1e6:.0f}M | chains=ETH,Base ══")
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
        chain  = token.get("chain", "ethereum")

        existing = store.get_token(addr)
        if existing.get("last_analyzed"):
            try:
                last = datetime.fromisoformat(existing["last_analyzed"])
                if (datetime.utcnow() - last).total_seconds() < 21600:
                    print(f"[Scanner] Skip {symbol} (<6h)")
                    continue
            except Exception:
                pass

        windows = score_manipulation_patterns(token)
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
            types   = ", ".join(set(w["type"] for w in windows))
            liq_m   = token.get("liq", 0) / 1e6
            cap_m   = token.get("market_cap", 0) / 1e6
            cap_str = f"cap=${cap_m:.1f}M" if cap_m > 0 else "cap=?"
            print(f"[Scanner] FLAGGED {symbol:12s} [{chain:8s}] "
                  f"liq=${liq_m:.1f}M {cap_str} | {types}")
        else:
            h1  = token.get("h1", 0) or 0
            h24 = token.get("h24", 0) or 0
            liq = token.get("liq", 0) or 0
            print(f"[Scanner] clean   {symbol:12s} [{chain:8s}] "
                  f"liq=${liq/1e3:.0f}K h1={h1:.1f}% h24={h24:.1f}%")

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
