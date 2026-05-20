"""
scanner/token_scanner.py  (v7 — mid/small cap manipulation focus)

TARGET PROFILE:
  - Market cap: $1M – $100M (manipulable range)
  - Liquidity:  $200K – $5M (thin enough to move, real enough to trade)
  - Volume:     $50K+ 24h (active trading)
  - CEX listed: yes (so you can trade futures)
  - NOT mega caps (UNI, LINK, AAVE etc — too much liquidity)

PATTERNS DETECTED:
  Original (from screenshots):
    P1 - GAS_FINGERPRINT_CLUSTER: identical gas params across unconnected wallets
    P2 - HUB_FANOUT: one wallet seeds multiple cluster members quickly

  New patterns we detect at scan time via price data:
    P3 - PUMP_DUMP_CYCLE: spike up then full retracement (repeated)
    P4 - SAWTOOTH: price oscillates in a band, never breaks out permanently
    P5 - LOW_FLOAT_SPIKE: tiny liquidity token with outsized volume spike
    P6 - REVERSAL_AFTER_PUMP: pumped >10% in h1/h6, now dumping in h24
    P7 - UNUSUAL_VOLUME: volume >> normal relative to liquidity (wash trading signal)
    P8 - MIDNIGHT_MOVER: consistent price moves in off-hours (bot behaviour)
"""

import os
import sys
import time
import random
import requests
from datetime import datetime, timedelta
from data.store import store
from scanner.cex_filter import filter_tokens

# ── Target profile thresholds ─────────────────────────────────────────────────
MIN_PUMP_PCT        = float(os.getenv("MIN_PRICE_CHANGE_PCT",   8))   # min % move to flag
MAX_TOKENS_TO_SCAN  = int(os.getenv("MAX_TOKENS_TO_SCAN",      30))

# Liquidity range we care about — too low = no CEX, too high = hard to manipulate
MIN_LIQUIDITY       = 100_000     # $100K minimum
MAX_LIQUIDITY       = 8_000_000   # $8M maximum (above this, hard to move)

# Volume/liquidity ratio above this = unusual activity
UNUSUAL_VOL_RATIO   = 3.0         # volume > 3x liquidity = suspicious

DS_BOOSTS_TOP    = "https://api.dexscreener.com/token-boosts/top/v1"
DS_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DS_SEARCH        = "https://api.dexscreener.com/latest/dex/search?q="
DS_TOKEN_PAIRS   = "https://api.dexscreener.com/latest/dex/tokens/"
DEFILLAMA_ALL    = "https://api.llama.fi/protocols"
CG_TRENDING      = "https://api.coingecko.com/api/v3/search/trending"

# DexScreener search works best with short token name/symbol fragments
# Multi-word natural language returns nothing — use 1-2 word terms
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

# ── HTTP helper ────────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 15) -> dict | list | None:
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "sentinel/2.0"})
        if r.status_code == 429:
            print(f"[Scanner] 429 skipped: {url[:50]}")
            return None
        if r.ok:
            try:
                return r.json()
            except Exception:
                # Large payload (DeFiLlama) sometimes truncates — try partial parse
                text = r.text
                # Find last complete JSON object/array
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


# ── Source A: DexScreener boosts ──────────────────────────────────────────────

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
                        "source":  "ds_boost",
                    })
        time.sleep(0.3)
    print(f"[Scanner] DS boosts: {len(results)}")
    return results


# ── Source B: DexScreener rotating search ─────────────────────────────────────

def _from_dexscreener_search() -> list[dict]:
    results = []
    queries = random.sample(SEARCH_QUERIES, min(10, len(SEARCH_QUERIES)))
    for q in queries:
        data = _get(DS_SEARCH + q.replace(" ", "%20"))
        if data:
            for p in (data.get("pairs") or [])[:25]:
                if p.get("chainId") != "ethereum":
                    continue
                base    = p.get("baseToken") or {}
                addr    = base.get("address", "").lower()
                sym     = base.get("symbol", "?")
                liq     = float((p.get("liquidity") or {}).get("usd", 0) or 0)
                vol     = float((p.get("volume") or {}).get("h24", 0) or 0)
                changes = p.get("priceChange") or {}

                # Pre-filter to target liquidity range
                if addr and MIN_LIQUIDITY <= liq <= MAX_LIQUIDITY:
                    results.append({
                        "address": addr, "symbol": sym, "source": "ds_search",
                        "liq": liq, "vol_24h": vol,
                        "h1":  float(changes.get("h1",  0) or 0),
                        "h6":  float(changes.get("h6",  0) or 0),
                        "h24": float(changes.get("h24", 0) or 0),
                        "d7":  float(changes.get("d7",  0) or 0) if "d7" in changes else None,
                    })
        time.sleep(0.3)

    seen, unique = set(), []
    for r in results:
        if r["address"] not in seen:
            seen.add(r["address"])
            unique.append(r)
    print(f"[Scanner] DS search ({','.join(queries[:3])}...): {len(unique)}")
    return unique


# ── Source C: DeFiLlama mid-cap protocols ─────────────────────────────────────

def _from_defillama_midcap() -> list[dict]:
    """
    DeFiLlama protocols filtered to mid-cap range and sorted by TVL change.
    Protocols with sudden TVL changes often correlate with token price moves.
    """
    results = []
    data = _get(DEFILLAMA_ALL, timeout=30)
    if not data or not isinstance(data, list):
        # Fallback: try their smaller summary endpoint
        data = _get("https://api.llama.fi/v2/protocols", timeout=20)
    if not data or not isinstance(data, list):
        print("[Scanner] DeFiLlama: no data")
        return []

    eth = [
        p for p in data
        if p.get("chain") == "Ethereum"
        and 50_000 < float(p.get("tvl", 0) or 0) < 50_000_000  # mid-cap TVL range
    ]
    # Sort by absolute 1h TVL change — biggest movers first
    eth.sort(key=lambda p: abs(float(p.get("change_1h", 0) or 0)), reverse=True)

    for p in eth[:60]:
        address = (p.get("address") or "").lower()
        symbol  = p.get("symbol", "?")
        change  = float(p.get("change_1h", 0) or 0)
        if address and address.startswith("0x") and len(address) == 42:
            results.append({
                "address":      address,
                "symbol":       symbol,
                "source":       "defillama_midcap",
                "defillama_1h": change,
            })

    print(f"[Scanner] DeFiLlama mid-cap: {len(results)}")
    return results


# ── Source D: CoinGecko trending (single call) ────────────────────────────────

def _from_coingecko_trending() -> list[dict]:
    results = []
    try:
        r = requests.get(CG_TRENDING, timeout=12,
                         headers={"User-Agent": "sentinel/2.0"})
        if r.status_code == 429:
            print("[Scanner] CG trending: 429, skipping")
            return []
        if r.ok:
            for coin in r.json().get("coins", [])[:20]:
                item     = coin.get("item", {})
                eth_addr = (item.get("platforms") or {}).get("ethereum", "").lower()
                # CoinGecko trending rank is a strong signal for manipulation target
                rank = item.get("market_cap_rank", 9999)
                # We want ranks 100–1000 — established but not mega cap
                if eth_addr and 50 <= rank <= 2000:
                    results.append({
                        "address": eth_addr,
                        "symbol":  item.get("symbol", "?").upper(),
                        "source":  "cg_trending",
                        "cg_rank": rank,
                    })
    except Exception as e:
        print(f"[Scanner] CG trending error: {e}")
    print(f"[Scanner] CG trending: {len(results)}")
    return results


# ── Source E: DexScreener high volume/liquidity ratio scan ────────────────────

def _from_unusual_volume_scan() -> list[dict]:
    """
    Scan for tokens where 24h volume >> liquidity.
    This ratio being >3x is a classic sign of wash trading or coordinated pumping.
    E.g. token has $500K liquidity but $2M volume = someone is churning it.
    """
    results = []
    # Search for recently active tokens
    queries = ["ethereum volume", "eth high volume", "ethereum active"]
    for q in random.sample(queries, 2):
        data = _get(DS_SEARCH + q.replace(" ", "%20"))
        if data:
            for p in (data.get("pairs") or [])[:30]:
                if p.get("chainId") != "ethereum":
                    continue
                base    = p.get("baseToken") or {}
                addr    = base.get("address", "").lower()
                sym     = base.get("symbol", "?")
                liq     = float((p.get("liquidity") or {}).get("usd", 0) or 0)
                vol     = float((p.get("volume") or {}).get("h24", 0) or 0)
                changes = p.get("priceChange") or {}

                if not addr or liq <= 0:
                    continue

                vol_ratio = vol / liq if liq > 0 else 0

                # Flag if: right liquidity range AND unusual volume ratio
                if (MIN_LIQUIDITY <= liq <= MAX_LIQUIDITY
                        and vol_ratio >= UNUSUAL_VOL_RATIO):
                    results.append({
                        "address":   addr, "symbol": sym,
                        "source":    "unusual_volume",
                        "liq":       liq, "vol_24h": vol,
                        "vol_ratio": round(vol_ratio, 1),
                        "h1":  float(changes.get("h1",  0) or 0),
                        "h6":  float(changes.get("h6",  0) or 0),
                        "h24": float(changes.get("h24", 0) or 0),
                        "d7":  float(changes.get("d7",  0) or 0) if "d7" in changes else None,
                    })
        time.sleep(0.3)

    seen, unique = set(), []
    for r in results:
        if r["address"] not in seen:
            seen.add(r["address"])
            unique.append(r)
    print(f"[Scanner] Unusual volume tokens: {len(unique)}")
    return unique


# ── Pattern detection ──────────────────────────────────────────────────────────

def score_manipulation_patterns(token: dict) -> list[dict]:
    """
    Score a token against all manipulation patterns.
    Returns list of detected pattern windows (empty = clean).

    Patterns checked:
      P3 - PUMP_DUMP_CYCLE     spike then full retracement
      P4 - SAWTOOTH            oscillates in band, never breaks
      P5 - LOW_FLOAT_SPIKE     huge move on low liquidity
      P6 - REVERSAL_AFTER_PUMP pumped h1/h6, reversing in h24
      P7 - UNUSUAL_VOLUME      vol >> liquidity (wash trading / churning)
      P8 - STRONG_MOVE_TARGET  strong enough move to be worth watching
    """
    addr   = token["address"]
    symbol = token.get("symbol", "?")
    now    = datetime.utcnow()

    # Fetch live price data if not pre-fetched
    h1  = token.get("h1")
    h6  = token.get("h6")
    h24 = token.get("h24")
    d7  = token.get("d7")
    liq = token.get("liq", 0)
    vol = token.get("vol_24h", 0)

    if h1 is None or h24 is None:
        data = _get(DS_TOKEN_PAIRS + addr)
        if not data:
            return []
        pairs     = data.get("pairs") or []
        eth_pairs = [p for p in pairs if p.get("chainId") == "ethereum"]
        if not eth_pairs:
            return []
        best    = max(eth_pairs,
                      key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
        changes = best.get("priceChange") or {}
        liq     = float((best.get("liquidity") or {}).get("usd", 0) or 0)
        vol     = float((best.get("volume") or {}).get("h24", 0) or 0)
        h1      = float(changes.get("h1",  0) or 0)
        h6      = float(changes.get("h6",  0) or 0)
        h24     = float(changes.get("h24", 0) or 0)
        d7      = float(changes.get("d7",  0) or 0) if "d7" in changes else None

    # Reject tokens outside target liquidity range
    if liq < MIN_LIQUIDITY or liq > MAX_LIQUIDITY:
        return []

    MIN     = MIN_PUMP_PCT
    windows = []
    vol_ratio = vol / liq if liq > 0 else 0

    base = {
        "token": addr, "symbol": symbol,
        "window_start": (now - timedelta(hours=25)).isoformat(),
        "window_end":   now.isoformat(),
        "liquidity_usd": liq, "volume_h24": vol,
    }

    # P3: Classic pump then dump — h1 or h6 pumped, h24 has reversed
    if h1 >= MIN and h24 < (h1 * 0.5):
        windows.append({**base, "type": "P3_pump_dump_h1",
                        "pump_pct": round(h1, 2),
                        "dump_pct": round(h1 - h24, 2)})

    if h6 >= MIN and h24 < (h6 * 0.5):
        windows.append({**base, "type": "P3_pump_dump_h6",
                        "pump_pct": round(h6, 2),
                        "dump_pct": round(h6 - h24, 2)})

    # P4: Sawtooth — significant intraday moves but flat over 7d
    # This is exactly the pattern from the screenshots: goes up 20-25%
    # then comes right back — repeated pattern suggests controlled manipulation
    if (d7 is not None
            and abs(d7) < 12          # flat over the week
            and abs(h24) >= MIN       # but significant daily move
            and abs(h1) >= MIN * 0.5):  # and still moving in h1
        windows.append({**base, "type": "P4_sawtooth_cycle",
                        "pump_pct": round(max(abs(h24), abs(h6 or 0)), 2),
                        "dump_pct": round(abs(d7), 2)})

    # P5: Low float spike — move much bigger than the liquidity would suggest
    # Small liquidity + big % move = small amount of money caused this
    if liq < 1_000_000 and abs(h1) >= MIN * 1.5:
        windows.append({**base, "type": "P5_low_float_spike",
                        "pump_pct": round(abs(h1), 2), "dump_pct": 0})

    # P6: Reversal pattern — was pumping (h6 positive) but now dumping (h24 < h6)
    # This means: someone pumped it hours ago, now they're exiting
    if h6 >= MIN and h24 < 0 and (h6 - h24) >= MIN * 1.5:
        windows.append({**base, "type": "P6_reversal_in_progress",
                        "pump_pct": round(h6, 2),
                        "dump_pct": round(h6 - h24, 2)})

    # P7: Wash trading / churning — volume far exceeds liquidity
    # This is a strong signal someone is artificially inflating activity
    if vol_ratio >= UNUSUAL_VOL_RATIO and abs(h24) >= MIN * 0.5:
        windows.append({**base, "type": "P7_unusual_volume_ratio",
                        "pump_pct": round(abs(h24), 2),
                        "dump_pct": round(vol_ratio, 1)})

    # P8: Strong move worth monitoring for the gas fingerprint pattern
    # Not necessarily a dump yet — but worth downloading transfer history
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
    print(f"\n[Scanner] ══ Scan {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ══")
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

        # Skip recently analyzed
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
            types = ", ".join(set(w["type"] for w in windows))
            liq_m = token.get("liq", 0) / 1e6
            print(f"[Scanner] FLAGGED {symbol:12s} "
                  f"liq=${liq_m:.1f}M | {types}")
        else:
            h1  = token.get("h1", 0) or 0
            h24 = token.get("h24", 0) or 0
            liq = token.get("liq", 0) or 0
            print(f"[Scanner] clean   {symbol:12s} "
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
