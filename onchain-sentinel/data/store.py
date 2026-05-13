"""
data/store.py
─────────────
In-memory signal store shared between the monitor and dashboard.
Thread-safe append-only log with basic querying.
"""

import threading
from datetime import datetime, timedelta
from typing import Optional


class SignalStore:
    def __init__(self, max_size: int = 500):
        self._signals  = []
        self._lock     = threading.Lock()
        self.max_size  = max_size

    def add(self, signal_dict: dict):
        with self._lock:
            self._signals.append({
                **signal_dict,
                "received_at": datetime.utcnow().isoformat(),
            })
            if len(self._signals) > self.max_size:
                self._signals = self._signals[-self.max_size:]

    def get_all(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(reversed(self._signals[-limit:]))

    def get_by_confidence(self, confidence: str) -> list[dict]:
        with self._lock:
            return [s for s in self._signals if s.get("confidence") == confidence]

    def get_last_24h(self) -> list[dict]:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        with self._lock:
            result = []
            for s in self._signals:
                try:
                    ts = datetime.fromisoformat(s["tx"]["timestamp"])
                    if ts >= cutoff:
                        result.append(s)
                except Exception:
                    pass
            return result

    def summary_stats(self) -> dict:
        with self._lock:
            total  = len(self._signals)
            high   = sum(1 for s in self._signals if s.get("confidence") == "high")
            medium = sum(1 for s in self._signals if s.get("confidence") == "medium")
            low    = sum(1 for s in self._signals if s.get("confidence") == "low")
            last24 = self.get_last_24h()
            return {
                "total_signals":   total,
                "high_confidence": high,
                "med_confidence":  medium,
                "low_confidence":  low,
                "last_24h":        len(last24),
                "last_signal_at":  self._signals[-1]["received_at"] if self._signals else None,
            }

    def clear(self):
        with self._lock:
            self._signals = []


# Global singleton
store = SignalStore()
