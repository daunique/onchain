"""
monitor/signal_engine.py  (v2 — ETH + Base)
─────────────────────────────────────────────
Staged signal detection. Chain-aware: signals carry a `chain` field and
hub/TRIO matching is scoped to wallets on the same chain.

Stage flow unchanged:
  S1_HUB_BROADCAST  → pre-dump staging detected
  S1_FANOUT_CONFIRMED → coordinated sweep confirmed
  CEX_DEPOSIT_WATCH  → dump imminent
"""

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

FANOUT_WINDOW_SECONDS = int(os.getenv("FANOUT_WINDOW_SECONDS", 300))
FANOUT_MIN_WALLETS    = int(os.getenv("FANOUT_MIN_WALLETS",    2))
CEX_WINDOW_HOURS      = int(os.getenv("CEX_WINDOW_HOURS",      12))

KNOWN_CEX_PREFIXES = {
    "bitget":  [],
    "bybit":   [],
    "okx":     [],
    "binance": [],
}

_pending_broadcasts: dict[str, dict] = {}
_fanout_tracker:     dict[str, list] = defaultdict(list)


class StagedSignal:
    def __init__(self, stage: str, confidence: float, token_symbol: str,
                 hub_address: str, trio_wallets: list[str],
                 tx_hash: str, block_number: int,
                 gas_fingerprint: dict, chain: str = "ethereum",
                 extra: dict = None):
        self.stage           = stage
        self.confidence      = confidence
        self.token_symbol    = token_symbol
        self.hub_address     = hub_address
        self.trio_wallets    = trio_wallets
        self.tx_hash         = tx_hash
        self.block_number    = block_number
        self.gas_fingerprint = gas_fingerprint
        self.chain           = chain
        self.extra           = extra or {}
        self.timestamp       = datetime.utcnow()

    def to_dict(self) -> dict:
        return {
            "stage":            self.stage,
            "confidence":       round(self.confidence, 2),
            "token_symbol":     self.token_symbol,
            "hub_address":      self.hub_address,
            "trio_wallets":     self.trio_wallets,
            "tx_hash":          self.tx_hash,
            "block_number":     self.block_number,
            "gas_fingerprint":  self.gas_fingerprint,
            "chain":            self.chain,
            "extra":            self.extra,
            "timestamp":        self.timestamp.isoformat(),
            "from_address":     self.hub_address,
            "to_address":       self.trio_wallets[0] if self.trio_wallets else "",
            "gas_limit":        self.gas_fingerprint.get("gas_limit", 0),
            "max_priority_fee": self.gas_fingerprint.get("max_priority_fee", 0),
            "max_fee":          self.gas_fingerprint.get("max_fee", 0),
            "pattern_label":    self.stage,
            "matched_rules":    self.extra.get("matched_rules", []),
            "score":            int(self.confidence * 100),
        }


class SignalEngine:
    def __init__(self, store, on_staged_signal):
        self.store            = store
        self.on_staged_signal = on_staged_signal
        self._tx_buffer: dict[str, list] = defaultdict(list)

    def process(self, tx: dict):
        self._buffer_tx(tx)
        self._check_hub_broadcast(tx)
        self._check_fanout(tx)

    def _buffer_tx(self, tx: dict):
        addr = tx.get("from_address", "").lower()
        if not addr:
            return
        now = datetime.utcnow()
        self._tx_buffer[addr].append({**tx, "_buffered_at": now})
        cutoff = now - timedelta(minutes=15)
        self._tx_buffer[addr] = [
            t for t in self._tx_buffer[addr]
            if t["_buffered_at"] >= cutoff
        ]

    def _check_hub_broadcast(self, tx: dict):
        from_addr = tx.get("from_address", "").lower()
        to_addr   = tx.get("to_address",   "").lower()
        tx_chain  = tx.get("chain", "ethereum")
        watchlist = self.store.get_watchlist()

        sender_meta = watchlist.get(from_addr)
        if not sender_meta:
            return

        # Scope to matching chain
        if sender_meta.get("chain", "ethereum") != tx_chain:
            return

        receiver_meta = watchlist.get(to_addr)
        if not receiver_meta:
            return
        if receiver_meta.get("chain", "ethereum") != tx_chain:
            return

        if sender_meta.get("token") != receiver_meta.get("token"):
            return

        fp     = sender_meta.get("fingerprint", {})
        symbol = sender_meta.get("symbol", "?")
        gl     = tx.get("gas_limit", 0)
        mpf    = tx.get("max_priority_fee", 0)
        mf     = tx.get("max_fee", 0)

        conf, rules = self._match_fingerprint(gl, mpf, mf, fp)
        conf = min(conf + 0.3, 0.97)
        rules.insert(0, f"HUB→TRIO transfer detected [{tx_chain}]")

        # Key broadcasts by chain:hub to avoid cross-chain collisions
        bcast_key = f"{tx_chain}:{from_addr}"
        _pending_broadcasts[bcast_key] = {
            "hub":          from_addr,
            "symbol":       symbol,
            "token":        sender_meta.get("token", ""),
            "chain":        tx_chain,
            "tx_hash":      tx.get("tx_hash", ""),
            "block_number": tx.get("block_number", 0),
            "fingerprint":  fp,
            "trio_seen":    [to_addr],
            "started_at":   datetime.utcnow(),
            "conf":         conf,
        }
        _fanout_tracker[bcast_key] = [tx]

        signal = StagedSignal(
            stage           = "S1_HUB_BROADCAST",
            confidence      = conf,
            token_symbol    = symbol,
            hub_address     = from_addr,
            trio_wallets    = [to_addr],
            tx_hash         = tx.get("tx_hash", ""),
            block_number    = tx.get("block_number", 0),
            gas_fingerprint = {"gas_limit": gl, "max_priority_fee": mpf, "max_fee": mf},
            chain           = tx_chain,
            extra={
                "matched_rules": rules,
                "lead_time_est": "12.6h empirical median to price trough",
                "watch_for":     ["fan-out to TRIO members in 5 min",
                                  "TRIO → CEX deposits in 6–12h"],
            }
        )
        self.on_staged_signal(signal)

    def _check_fanout(self, tx: dict):
        from_addr = tx.get("from_address", "").lower()
        to_addr   = tx.get("to_address",   "").lower()
        tx_chain  = tx.get("chain", "ethereum")
        watchlist = self.store.get_watchlist()
        now       = datetime.utcnow()

        for bcast_key, broadcast in list(_pending_broadcasts.items()):
            if broadcast.get("chain") != tx_chain:
                continue

            elapsed = (now - broadcast["started_at"]).total_seconds()
            if elapsed > FANOUT_WINDOW_SECONDS * 3:
                del _pending_broadcasts[bcast_key]
                continue

            if from_addr not in broadcast["trio_seen"]:
                continue
            if not watchlist.get(to_addr):
                continue
            if watchlist.get(to_addr, {}).get("chain", "ethereum") != tx_chain:
                continue

            _fanout_tracker[bcast_key].append(tx)
            if to_addr not in broadcast["trio_seen"]:
                broadcast["trio_seen"].append(to_addr)

            fanout_count = len([
                t for t in _fanout_tracker[bcast_key]
                if t.get("from_address", "").lower() in broadcast["trio_seen"]
                and t["from_address"] != broadcast["hub"]
            ])

            if fanout_count >= FANOUT_MIN_WALLETS:
                elapsed_s = int((now - broadcast["started_at"]).total_seconds())
                conf      = min(broadcast["conf"] + 0.15, 0.99)
                fp        = broadcast["fingerprint"]

                signal = StagedSignal(
                    stage           = "S1_FANOUT_CONFIRMED",
                    confidence      = conf,
                    token_symbol    = broadcast["symbol"],
                    hub_address     = broadcast["hub"],
                    trio_wallets    = broadcast["trio_seen"],
                    tx_hash         = tx.get("tx_hash", ""),
                    block_number    = tx.get("block_number", 0),
                    gas_fingerprint = fp,
                    chain           = tx_chain,
                    extra={
                        "matched_rules": [
                            f"Fan-out confirmed: {elapsed_s}s after broadcast",
                            "Coordinated multi-wallet sweep — not human behaviour",
                            f"{len(broadcast['trio_seen'])} TRIO wallets active",
                        ],
                        "trade_action": "Open SHORT now",
                        "trade_window": "0–12h",
                        "watch_for":    ["TRIO → CEX deposits in 6–12h"],
                        "elapsed_s":    elapsed_s,
                    }
                )
                self.on_staged_signal(signal)
                del _pending_broadcasts[bcast_key]
                break

    def _match_fingerprint(self, gl: int, mpf: float, mf: float,
                           expected: dict) -> tuple[float, list[str]]:
        score = 0.0
        rules = []
        tol   = float(os.getenv("GAS_FUZZY_TOLERANCE_PCT", 5)) / 100

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
