"""
analyzer/clustering.py
───────────────────────
Automatically clusters wallets by shared gas fingerprints.

Algorithm:
  1. Extract all (gas_limit, max_priority_fee, max_fee) tuples from transfers
  2. Group wallets that share the same or very similar gas settings
  3. Filter out clusters that are too small or look like normal behaviour
  4. Score each cluster by coordination strength
  5. Return suspicious clusters + the fingerprint they share

No manual input needed — the system discovers patterns from data.
"""

import os
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

FUZZY_TOLERANCE = float(os.getenv("GAS_FUZZY_TOLERANCE_PCT", 5)) / 100
MIN_CLUSTER_SIZE = int(os.getenv("MIN_CLUSTER_SIZE", 2))

# Gas limits that are completely standard — skip these, they're not fingerprints
COMMON_GAS_LIMITS = {21000, 65000, 100000}


@dataclass
class GasFingerprint:
    gas_limit:        int
    max_priority_fee: float
    max_fee:          float

    def is_standard(self) -> bool:
        """Standard/default gas settings used by most wallets — not suspicious."""
        return (
            self.gas_limit in COMMON_GAS_LIMITS
            or (self.max_priority_fee == 0 and self.max_fee == 0)
        )

    def matches(self, other: "GasFingerprint") -> bool:
        """Fuzzy match within tolerance."""
        def close(a, b):
            if b == 0:
                return a == 0
            return abs(a - b) / b <= FUZZY_TOLERANCE

        return (
            close(self.gas_limit, other.gas_limit)
            and close(self.max_priority_fee, other.max_priority_fee)
            and close(self.max_fee, other.max_fee)
        )

    def to_dict(self):
        return asdict(self)


@dataclass
class WalletCluster:
    token:       str
    symbol:      str
    fingerprint: GasFingerprint
    wallets:     list[str]          = field(default_factory=list)
    tx_count:    int                = 0
    total_volume: float             = 0.0
    first_seen:  Optional[str]      = None
    last_seen:   Optional[str]      = None
    score:       int                = 0
    label:       str                = "unclassified"

    def to_dict(self):
        return {
            "token":       self.token,
            "symbol":      self.symbol,
            "fingerprint": self.fingerprint.to_dict(),
            "wallets":     self.wallets,
            "tx_count":    self.tx_count,
            "total_volume": self.total_volume,
            "first_seen":  self.first_seen,
            "last_seen":   self.last_seen,
            "score":       self.score,
            "label":       self.label,
            "wallet_count": len(self.wallets),
        }


def extract_fingerprint(tx: dict) -> Optional[GasFingerprint]:
    """Build a GasFingerprint from a raw tx dict."""
    try:
        gl  = int(tx.get("gas_limit", 0) or 0)
        mpf = float(tx.get("max_priority_fee", 0) or 0)
        mf  = float(tx.get("max_fee", 0) or 0)
        if gl == 0:
            return None
        return GasFingerprint(gas_limit=gl, max_priority_fee=mpf, max_fee=mf)
    except Exception:
        return None


def cluster_wallets(transfers: list[dict], token_address: str, symbol: str) -> list[WalletCluster]:
    """
    Main clustering function.
    
    Takes a list of enriched transfer dicts and returns suspicious wallet clusters.
    Each cluster = a group of wallets sharing the same gas fingerprint.
    """
    print(f"[Cluster] Analysing {len(transfers)} transfers for {symbol}...")

    # Step 1: Build wallet → fingerprint mapping
    # A wallet may use different gas settings; we track all unique ones
    wallet_fingerprints: dict[str, list[GasFingerprint]] = defaultdict(list)
    wallet_meta: dict[str, dict] = defaultdict(lambda: {"tx_count": 0, "volume": 0.0, "timestamps": []})

    for tx in transfers:
        wallet = tx.get("from", "").lower()
        if not wallet:
            continue

        fp = extract_fingerprint(tx)
        if fp is None or fp.is_standard():
            continue

        wallet_fingerprints[wallet].append(fp)
        wallet_meta[wallet]["tx_count"] += 1
        try:
            wallet_meta[wallet]["volume"] += float(tx.get("value", 0) or 0) / 1e18
        except Exception:
            pass
        try:
            wallet_meta[wallet]["timestamps"].append(int(tx.get("timeStamp", 0) or 0))
        except Exception:
            pass

    print(f"[Cluster] {len(wallet_fingerprints)} wallets with non-standard gas settings")

    # Step 2: Build fingerprint → wallets mapping using fuzzy grouping
    # We do a simple O(n²) pass — fine for hundreds of wallets
    fingerprint_groups: list[dict] = []

    def find_group(fp: GasFingerprint):
        for g in fingerprint_groups:
            if g["fingerprint"].matches(fp):
                return g
        return None

    for wallet, fps in wallet_fingerprints.items():
        # Use the most common fingerprint for this wallet
        fp = _most_common_fingerprint(fps)
        if fp is None:
            continue

        group = find_group(fp)
        if group:
            group["wallets"].add(wallet)
            group["fps"].append(fp)
        else:
            fingerprint_groups.append({
                "fingerprint": fp,
                "wallets":     {wallet},
                "fps":         [fp],
            })

    print(f"[Cluster] {len(fingerprint_groups)} distinct fingerprint groups")

    # Step 3: Filter to suspicious clusters (>= MIN_CLUSTER_SIZE)
    clusters: list[WalletCluster] = []

    for group in fingerprint_groups:
        wallets = list(group["wallets"])
        if len(wallets) < MIN_CLUSTER_SIZE:
            continue

        fp = group["fingerprint"]

        # Aggregate metadata
        tx_count    = sum(wallet_meta[w]["tx_count"] for w in wallets)
        total_vol   = sum(wallet_meta[w]["volume"]   for w in wallets)
        all_ts      = []
        for w in wallets:
            all_ts.extend(wallet_meta[w]["timestamps"])
        all_ts.sort()

        first_seen = datetime.utcfromtimestamp(all_ts[0]).isoformat()  if all_ts else None
        last_seen  = datetime.utcfromtimestamp(all_ts[-1]).isoformat() if all_ts else None

        # Score the cluster
        score, label = _score_cluster(wallets, fp, tx_count, total_vol)

        cluster = WalletCluster(
            token        = token_address.lower(),
            symbol       = symbol,
            fingerprint  = fp,
            wallets      = wallets,
            tx_count     = tx_count,
            total_volume = round(total_vol, 4),
            first_seen   = first_seen,
            last_seen    = last_seen,
            score        = score,
            label        = label,
        )
        clusters.append(cluster)
        print(f"[Cluster] ✅ Cluster: {len(wallets)} wallets | GL={fp.gas_limit} | "
              f"PF={fp.max_priority_fee}gwei | MF={fp.max_fee}gwei | score={score}")

    # Sort by score descending
    clusters.sort(key=lambda c: c.score, reverse=True)
    print(f"[Cluster] Found {len(clusters)} suspicious clusters for {symbol}")
    return clusters


def _most_common_fingerprint(fps: list[GasFingerprint]) -> Optional[GasFingerprint]:
    if not fps:
        return None
    # Return the fingerprint that appears most often (exact match)
    counts = defaultdict(int)
    for fp in fps:
        key = (fp.gas_limit, fp.max_priority_fee, fp.max_fee)
        counts[key] += 1
    best = max(counts, key=counts.get)
    return GasFingerprint(*best)


def _score_cluster(wallets: list[str], fp: GasFingerprint, tx_count: int, volume: float) -> tuple[int, str]:
    """
    Score a cluster 0–100 based on how suspicious the coordination looks.
    """
    score = 0
    notes = []

    # Wallet count
    n = len(wallets)
    if n >= 10: score += 30; notes.append("large_group")
    elif n >= 5: score += 20; notes.append("medium_group")
    elif n >= 2: score += 10; notes.append("small_group")

    # Unusual gas limit (not a round number commonly used)
    if fp.gas_limit not in {50000, 100000, 150000, 200000, 250000, 300000}:
        score += 15; notes.append("unusual_gas_limit")
    else:
        score += 10  # round number but still notable

    # Non-zero EIP-1559 fee settings (shows intentional config, not default)
    if fp.max_priority_fee > 0 and fp.max_fee > 0:
        score += 20; notes.append("explicit_eip1559_fees")

    # Very specific fee ratio (e.g. max_fee = 2x priority — exactly)
    if fp.max_priority_fee > 0:
        ratio = fp.max_fee / fp.max_priority_fee
        if 1.9 <= ratio <= 2.1:
            score += 15; notes.append("exact_2x_fee_ratio")

    # Transaction volume (high volume = more significant)
    if tx_count >= 20: score += 10
    elif tx_count >= 5: score += 5

    label = "_".join(notes) if notes else "coordinated_gas"
    return min(score, 100), label
