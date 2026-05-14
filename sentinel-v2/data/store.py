"""
data/store.py
─────────────
Central in-memory state for the entire system.
Holds discovered tokens, pump/dump events, wallet clusters, watchlist, and live signals.
Thread-safe. Persists to JSON on disk so restarts don't lose state.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

STATE_FILE = Path("data/state.json")


class SentinelStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.tokens: dict[str, dict]    = {}   # contract → token info
        self.pump_events: list[dict]    = []   # detected pump/dump windows
        self.clusters: list[dict]       = []   # wallet clusters per token
        self.watchlist: dict[str, dict] = {}   # address → wallet meta
        self.signals: list[dict]        = []   # live match signals
        self.scan_log: list[dict]       = []   # scanner run history
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────
    def _load(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    d = json.load(f)
                self.tokens      = d.get("tokens", {})
                self.pump_events = d.get("pump_events", [])
                self.clusters    = d.get("clusters", [])
                self.watchlist   = d.get("watchlist", {})
                self.signals     = d.get("signals", [])[-200:]
                self.scan_log    = d.get("scan_log", [])[-50:]
                print(f"[Store] Loaded state: {len(self.tokens)} tokens, "
                      f"{len(self.watchlist)} watched wallets, "
                      f"{len(self.signals)} signals")
            except Exception as e:
                print(f"[Store] Load error (starting fresh): {e}")

    def save(self):
        STATE_FILE.parent.mkdir(exist_ok=True)
        with self._lock:
            try:
                with open(STATE_FILE, "w") as f:
                    json.dump({
                        "tokens":      self.tokens,
                        "pump_events": self.pump_events,
                        "clusters":    self.clusters,
                        "watchlist":   self.watchlist,
                        "signals":     self.signals[-200:],
                        "scan_log":    self.scan_log[-50:],
                    }, f, indent=2, default=str)
            except Exception as e:
                print(f"[Store] Save error: {e}")

    # ── Tokens ────────────────────────────────────────────────────────────
    def upsert_token(self, address: str, info: dict):
        with self._lock:
            self.tokens[address.lower()] = {
                **(self.tokens.get(address.lower(), {})),
                **info,
                "updated_at": datetime.utcnow().isoformat(),
            }

    def get_token(self, address: str) -> dict:
        return self.tokens.get(address.lower(), {})

    # ── Pump events ───────────────────────────────────────────────────────
    def add_pump_event(self, event: dict):
        with self._lock:
            self.pump_events.append({**event, "detected_at": datetime.utcnow().isoformat()})

    def get_pump_events(self, token_address: str = None) -> list[dict]:
        with self._lock:
            if token_address:
                return [e for e in self.pump_events if e.get("token") == token_address.lower()]
            return list(self.pump_events)

    # ── Clusters ──────────────────────────────────────────────────────────
    def add_cluster(self, cluster: dict):
        with self._lock:
            self.clusters.append({**cluster, "created_at": datetime.utcnow().isoformat()})

    def get_clusters(self, token_address: str = None) -> list[dict]:
        with self._lock:
            if token_address:
                return [c for c in self.clusters if c.get("token") == token_address.lower()]
            return list(self.clusters)

    # ── Watchlist ─────────────────────────────────────────────────────────
    def add_to_watchlist(self, address: str, meta: dict):
        addr = address.lower()
        with self._lock:
            existing = self.watchlist.get(addr, {})
            self.watchlist[addr] = {
                **existing,
                **meta,
                "address":    addr,
                "added_at":   existing.get("added_at", datetime.utcnow().isoformat()),
                "hit_count":  existing.get("hit_count", 0),
            }

    def get_watchlist(self) -> dict:
        with self._lock:
            return dict(self.watchlist)

    def increment_hit(self, address: str):
        addr = address.lower()
        with self._lock:
            if addr in self.watchlist:
                self.watchlist[addr]["hit_count"] = self.watchlist[addr].get("hit_count", 0) + 1
                self.watchlist[addr]["last_seen"] = datetime.utcnow().isoformat()

    # ── Signals ───────────────────────────────────────────────────────────
    def add_signal(self, signal: dict):
        with self._lock:
            self.signals.append({**signal, "received_at": datetime.utcnow().isoformat()})
            if len(self.signals) > 500:
                self.signals = self.signals[-500:]

    def get_signals(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(reversed(self.signals[-limit:]))

    # ── Scan log ──────────────────────────────────────────────────────────
    def log_scan(self, entry: dict):
        with self._lock:
            self.scan_log.append({**entry, "at": datetime.utcnow().isoformat()})

    # ── Summary stats ─────────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            return {
                "tokens_tracked":    len(self.tokens),
                "pump_events_found": len(self.pump_events),
                "clusters_found":    len(self.clusters),
                "wallets_watched":   len(self.watchlist),
                "total_signals":     len(self.signals),
                "high_signals":      sum(1 for s in self.signals if s.get("confidence") == "high"),
                "med_signals":       sum(1 for s in self.signals if s.get("confidence") == "medium"),
                "low_signals":       sum(1 for s in self.signals if s.get("confidence") == "low"),
                "last_scan":         self.scan_log[-1]["at"] if self.scan_log else None,
                "last_signal":       self.signals[-1]["received_at"] if self.signals else None,
            }


store = SentinelStore()
