"""
monitor/signal_engine.py
─────────────────────────
Staged signal logic modelled on the real alert flow seen in the screenshots:

  Stage 1 — S1_HUB_BROADCAST
    A "hub" wallet (cluster coordinator) sends tokens to multiple
    cluster members (TRIO wallets) within a short window.
    → BEARISH signal, pre-dump staging detected.
    → Lead time to price trough: ~12h empirically.

  Stage 2 — S1_FANOUT_CONFIRMED
    Within 5 min of the hub broadcast, 2+ TRIO wallets
    redistribute tokens onward (fan-out sweep).
    → Confirms coordinated multi-wallet behaviour, not human.
    → Open SHORT now. Window: 0–12h.

  Stage 3 — CEX_DEPOSIT_WATCH
    TRIO wallets start sending to known CEX deposit addresses.
    → Final confirmation. Dump imminent on CEX.

Each stage upgrades confidence and triggers a progressively
more urgent Telegram alert.
"""

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

FANOUT_WINDOW_SECONDS  = int(os.getenv("FANOUT_WINDOW_SECONDS",  300))   # 5 min
FANOUT_MIN_WALLETS     = int(os.getenv("FANOUT_MIN_WALLETS",     2))     # min TRIO recipients
CEX_WINDOW_HOURS       = int(os.getenv("CEX_WINDOW_HOURS",       12))

# Known CEX hot/deposit address prefixes — expand as you discover more
# These are illustrative; the system also learns them from cluster analysis
KNOWN_CEX_PREFIXES = {
    "bitget":  [],   # add known Bitget deposit routers
    "bybit":   [],
    "okx":     [],
    "binance": [],
}

# In-memory staging state: tracks active broadcast events awaiting fan-out
_pending_broadcasts: dict[str, dict] = {}   # hub_address → event
_fanout_tracker:     dict[str, list] = defaultdict(list)  # hub_address → [fanout_txs]


class StagedSignal:
    def __init__(self, stage: str, confidence: float, token_symbol: str,
                 hub_address: str, trio_wallets: list[str],
                 tx_hash: str, block_number: int,
                 gas_fingerprint: dict, extra: dict = None):
        self.stage           = stage         # S1_HUB_BROADCAST | S1_FANOUT_CONFIRMED | CEX_DEPOSIT
        self.confidence      = confidence    # 0.0 – 1.0
        self.token_symbol    = token_symbol
        self.hub_address     = hub_address
        self.trio_wallets    = trio_wallets
        self.tx_hash         = tx_hash
        self.block_number    = block_number
        self.gas_fingerprint = gas_fingerprint
        self.extra           = extra or {}
        self.timestamp       = datetime.utcnow()

    def to_dict(self) -> dict:
        return {
            "stage":           self.stage,
            "confidence":      round(self.confidence, 2),
            "token_symbol":    self.token_symbol,
            "hub_address":     self.hub_address,
            "trio_wallets":    self.trio_wallets,
            "tx_hash":         self.tx_hash,
            "block_number":    self.block_number,
            "gas_fingerprint": self.gas_fingerprint,
            "extra":           self.extra,
            "timestamp":       self.timestamp.isoformat(),
            # Dashboard display fields
            "from_address":    self.hub_address,
            "to_address":      self.trio_wallets[0] if self.trio_wallets else "",
            "gas_limit":       self.gas_fingerprint.get("gas_limit", 0),
            "max_priority_fee": self.gas_fingerprint.get("max_priority_fee", 0),
            "max_fee":         self.gas_fingerprint.get("max_fee", 0),
            "pattern_label":   self.stage,
            "matched_rules":   self.extra.get("matched_rules", []),
            "score":           int(self.confidence * 100),
        }


class SignalEngine:
    """
    Receives every token transfer from the live monitor,
    runs it through staged detection logic,
    and fires callbacks when a stage is confirmed.
    """

    def __init__(self, store, on_staged_signal):
        self.store            = store
        self.on_staged_signal = on_staged_signal
        # tx buffer: from_address → list of recent txs (last 10 min)
        self._tx_buffer: dict[str, list] = defaultdict(list)

    def process(self, tx: dict):
        """Entry point: called for every token transfer."""
        self._buffer_tx(tx)
        self._check_hub_broadcast(tx)
        self._check_fanout(tx)

    def _buffer_tx(self, tx: dict):
        """Keep a short rolling buffer of recent txs per sender."""
        addr = tx.get("from_address", "").lower()
        if not addr:
            return
        now = datetime.utcnow()
        self._tx_buffer[addr].append({**tx, "_buffered_at": now})
        # Purge entries older than 15 min
        cutoff = now - timedelta(minutes=15)
        self._tx_buffer[addr] = [
            t for t in self._tx_buffer[addr]
            if t["_buffered_at"] >= cutoff
        ]

    # ── Stage 1: HUB_BROADCAST ────────────────────────────────────────────────

    def _check_hub_broadcast(self, tx: dict):
        """
        Detect a hub wallet sending the fingerprinted token to cluster members.
        Fires S1_HUB_BROADCAST and arms fan-out detection.
        """
        from_addr = tx.get("from_address", "").lower()
        to_addr   = tx.get("to_address", "").lower()
        watchlist = self.store.get_watchlist()

        # Sender must be a known cluster wallet
        sender_meta = watchlist.get(from_addr)
        if not sender_meta:
            return

        # Receiver must also be a known cluster wallet (HUB → TRIO)
        receiver_meta = watchlist.get(to_addr)
        if not receiver_meta:
            return

        # Must be same cluster (same token)
        if sender_meta.get("token") != receiver_meta.get("token"):
            return

        fp       = sender_meta.get("fingerprint", {})
        symbol   = sender_meta.get("symbol", "?")
        gl       = tx.get("gas_limit", 0)
        mpf      = tx.get("max_priority_fee", 0)
        mf       = tx.get("max_fee", 0)

        # Compute confidence based on gas match
        conf, rules = self._match_fingerprint(gl, mpf, mf, fp)
        # Base confidence for being a watched→watched transfer
        conf = min(conf + 0.3, 0.97)
        rules.insert(0, f"HUB→TRIO transfer detected")

        # Record this broadcast for fan-out tracking
        _pending_broadcasts[from_addr] = {
            "hub":          from_addr,
            "symbol":       symbol,
            "token":        sender_meta.get("token", ""),
            "tx_hash":      tx.get("tx_hash", ""),
            "block_number": tx.get("block_number", 0),
            "fingerprint":  fp,
            "trio_seen":    [to_addr],
            "started_at":   datetime.utcnow(),
            "conf":         conf,
        }
        _fanout_tracker[from_addr] = [tx]

        signal = StagedSignal(
            stage           = "S1_HUB_BROADCAST",
            confidence      = conf,
            token_symbol    = symbol,
            hub_address     = from_addr,
            trio_wallets    = [to_addr],
            tx_hash         = tx.get("tx_hash", ""),
            block_number    = tx.get("block_number", 0),
            gas_fingerprint = {"gas_limit": gl, "max_priority_fee": mpf, "max_fee": mf},
            extra={
                "matched_rules":  rules,
                "lead_time_est":  "12.6h empirical median to price trough",
                "watch_for":      ["fan-out to TRIO members in 5 min",
                                   "TRIO → CEX deposits in 6–12h"],
            }
        )
        self.on_staged_signal(signal)

    # ── Stage 2: FANOUT_CONFIRMED ─────────────────────────────────────────────

    def _check_fanout(self, tx: dict):
        """
        After a HUB_BROADCAST, watch for the TRIO wallets redistributing
        tokens onward to more cluster members within FANOUT_WINDOW_SECONDS.
        Fires S1_FANOUT_CONFIRMED when 2+ TRIO wallets have forwarded.
        """
        from_addr = tx.get("from_address", "").lower()
        to_addr   = tx.get("to_address", "").lower()
        watchlist = self.store.get_watchlist()
        now       = datetime.utcnow()

        # Look for any active broadcast where this sender is a TRIO member
        for hub_addr, broadcast in list(_pending_broadcasts.items()):
            # Check window hasn't expired
            elapsed = (now - broadcast["started_at"]).total_seconds()
            if elapsed > FANOUT_WINDOW_SECONDS * 3:  # 15 min max window
                del _pending_broadcasts[hub_addr]
                continue

            # This tx is from a TRIO member of this broadcast
            if from_addr not in broadcast["trio_seen"]:
                continue

            # Receiver is also a cluster wallet
            if not watchlist.get(to_addr):
                continue

            # Add to fanout tracker
            _fanout_tracker[hub_addr].append(tx)
            if to_addr not in broadcast["trio_seen"]:
                broadcast["trio_seen"].append(to_addr)

            # Fan-out confirmed when 2+ TRIO members have forwarded
            fanout_count = len([
                t for t in _fanout_tracker[hub_addr]
                if t.get("from_address", "").lower() in broadcast["trio_seen"]
                and t["from_address"] != hub_addr
            ])

            if fanout_count >= FANOUT_MIN_WALLETS:
                elapsed_s  = int((now - broadcast["started_at"]).total_seconds())
                conf       = min(broadcast["conf"] + 0.15, 0.99)
                fp         = broadcast["fingerprint"]

                signal = StagedSignal(
                    stage           = "S1_FANOUT_CONFIRMED",
                    confidence      = conf,
                    token_symbol    = broadcast["symbol"],
                    hub_address     = hub_addr,
                    trio_wallets    = broadcast["trio_seen"],
                    tx_hash         = tx.get("tx_hash", ""),
                    block_number    = tx.get("block_number", 0),
                    gas_fingerprint = fp,
                    extra={
                        "matched_rules": [
                            f"Fan-out confirmed: {elapsed_s}s after broadcast",
                            "Coordinated multi-wallet sweep — not human behaviour",
                            f"{len(broadcast['trio_seen'])} TRIO wallets active",
                        ],
                        "trade_action":  "Open SHORT now",
                        "trade_window":  "0–12h",
                        "watch_for":     ["TRIO → CEX deposits in 6–12h"],
                        "elapsed_s":     elapsed_s,
                    }
                )
                self.on_staged_signal(signal)
                # Remove so we don't double-fire
                del _pending_broadcasts[hub_addr]
                break

    # ── Fingerprint matcher ───────────────────────────────────────────────────

    def _match_fingerprint(self, gl: int, mpf: float, mf: float,
                           expected: dict) -> tuple[float, list[str]]:
        """Score a live tx gas against the expected fingerprint."""
        score  = 0.0
        rules  = []
        tol    = float(os.getenv("GAS_FUZZY_TOLERANCE_PCT", 5)) / 100

        def close(a, b):
            if b == 0: return a == 0
            return abs(a - b) / b <= tol

        exp_gl  = expected.get("gas_limit", 0)
        exp_mpf = expected.get("max_priority_fee", 0)
        exp_mf  = expected.get("max_fee", 0)

        if exp_gl:
            if gl == exp_gl:
                score += 0.40; rules.append(f"exact gas_limit={exp_gl:,}")
            elif close(gl, exp_gl):
                score += 0.20; rules.append(f"fuzzy gas_limit≈{exp_gl:,}")

        if exp_mpf:
            if mpf == exp_mpf:
                score += 0.30; rules.append(f"exact priority_fee={exp_mpf}gwei")
            elif close(mpf, exp_mpf):
                score += 0.15; rules.append(f"fuzzy priority_fee≈{exp_mpf}gwei")

        if exp_mf:
            if mf == exp_mf:
                score += 0.30; rules.append(f"exact max_fee={exp_mf}gwei")
            elif close(mf, exp_mf):
                score += 0.15; rules.append(f"fuzzy max_fee≈{exp_mf}gwei")

        return min(score, 1.0), rules
