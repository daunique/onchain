"""
engine/fingerprint.py
─────────────────────
Gas fingerprint pattern detection engine.
Identifies coordinated wallets by matching transaction metadata patterns.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime


# ── Default fingerprint loaded from env or hardcoded from your analysis ──
DEFAULT_FINGERPRINT = {
    "gas_limit":            200000,
    "max_priority_fee_gwei": 3,
    "max_fee_gwei":          6,
}

# Tolerance for fuzzy matching (e.g. ±5% on gas limit)
FUZZY_TOLERANCE = 0.05


@dataclass
class TransactionData:
    tx_hash:             str
    from_address:        str
    to_address:          str
    token_amount:        float
    gas_limit:           int
    max_priority_fee:    float   # in GWEI
    max_fee:             float   # in GWEI
    timestamp:           datetime
    block_number:        int
    chain:               str = "ethereum"

    def to_dict(self):
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class SignalResult:
    tx:               TransactionData
    confidence:       str            # "low" | "medium" | "high"
    matched_rules:    list[str]      = field(default_factory=list)
    score:            int            = 0
    is_known_wallet:  bool           = False
    pattern_label:    str            = "unknown"

    def to_dict(self):
        return {
            "tx":             self.tx.to_dict(),
            "confidence":     self.confidence,
            "matched_rules":  self.matched_rules,
            "score":          self.score,
            "is_known_wallet": self.is_known_wallet,
            "pattern_label":  self.pattern_label,
        }


class FingerprintEngine:
    """
    Compares incoming transactions against known fingerprint profiles.
    Supports exact matching + fuzzy tolerance + multi-fingerprint scoring.
    """

    def __init__(self, fingerprints: Optional[list[dict]] = None,
                 known_wallets: Optional[list[str]] = None):

        self.fingerprints  = fingerprints or [DEFAULT_FINGERPRINT]
        self.known_wallets = set(w.lower() for w in (known_wallets or []))
        self.signal_log: list[SignalResult] = []

    # ── Add/remove fingerprints at runtime ───────────────────────────────
    def add_fingerprint(self, fp: dict):
        self.fingerprints.append(fp)

    def add_known_wallet(self, address: str):
        self.known_wallets.add(address.lower())

    # ── Core matching logic ───────────────────────────────────────────────
    def _fuzzy_match(self, value: float, target: float) -> bool:
        if target == 0:
            return value == 0
        return abs(value - target) / target <= FUZZY_TOLERANCE

    def _score_transaction(self, tx: TransactionData) -> tuple[int, list[str]]:
        score = 0
        matched = []

        for fp in self.fingerprints:
            fp_score  = 0
            fp_matched = []

            # Gas limit check
            if "gas_limit" in fp:
                if tx.gas_limit == fp["gas_limit"]:
                    fp_score += 40
                    fp_matched.append(f"exact gas_limit={fp['gas_limit']}")
                elif self._fuzzy_match(tx.gas_limit, fp["gas_limit"]):
                    fp_score += 20
                    fp_matched.append(f"fuzzy gas_limit≈{fp['gas_limit']}")

            # Max priority fee check
            if "max_priority_fee_gwei" in fp:
                if tx.max_priority_fee == fp["max_priority_fee_gwei"]:
                    fp_score += 30
                    fp_matched.append(f"exact priority_fee={fp['max_priority_fee_gwei']}gwei")
                elif self._fuzzy_match(tx.max_priority_fee, fp["max_priority_fee_gwei"]):
                    fp_score += 15
                    fp_matched.append(f"fuzzy priority_fee≈{fp['max_priority_fee_gwei']}gwei")

            # Max fee check
            if "max_fee_gwei" in fp:
                if tx.max_fee == fp["max_fee_gwei"]:
                    fp_score += 30
                    fp_matched.append(f"exact max_fee={fp['max_fee_gwei']}gwei")
                elif self._fuzzy_match(tx.max_fee, fp["max_fee_gwei"]):
                    fp_score += 15
                    fp_matched.append(f"fuzzy max_fee≈{fp['max_fee_gwei']}gwei")

            score   = max(score, fp_score)
            matched = fp_matched if fp_score > score else matched

        # Bonus: known wallet
        if tx.from_address.lower() in self.known_wallets:
            score += 25
            matched.append("known_manipulator_wallet")

        return score, matched

    def _confidence_label(self, score: int) -> str:
        if score >= 80:
            return "high"
        if score >= 45:
            return "medium"
        return "low"

    def _pattern_label(self, matched: list[str]) -> str:
        if "known_manipulator_wallet" in matched:
            return "known_actor_transfer"
        if any("exact" in m for m in matched):
            return "exact_gas_fingerprint"
        if any("fuzzy" in m for m in matched):
            return "fuzzy_gas_fingerprint"
        return "no_match"

    # ── Public API ────────────────────────────────────────────────────────
    def analyze(self, tx: TransactionData) -> Optional[SignalResult]:
        """
        Returns a SignalResult if the transaction matches any pattern,
        or None if no match found.
        """
        score, matched = self._score_transaction(tx)

        if score == 0:
            return None

        result = SignalResult(
            tx=tx,
            confidence=self._confidence_label(score),
            matched_rules=matched,
            score=score,
            is_known_wallet=tx.from_address.lower() in self.known_wallets,
            pattern_label=self._pattern_label(matched),
        )

        self.signal_log.append(result)
        return result

    def get_signal_history(self, limit: int = 50) -> list[dict]:
        return [s.to_dict() for s in self.signal_log[-limit:]]

    def get_stats(self) -> dict:
        total  = len(self.signal_log)
        high   = sum(1 for s in self.signal_log if s.confidence == "high")
        medium = sum(1 for s in self.signal_log if s.confidence == "medium")
        low    = sum(1 for s in self.signal_log if s.confidence == "low")
        return {
            "total_signals":   total,
            "high_confidence": high,
            "med_confidence":  medium,
            "low_confidence":  low,
            "known_wallets_tracked": len(self.known_wallets),
            "fingerprints_loaded":   len(self.fingerprints),
        }
