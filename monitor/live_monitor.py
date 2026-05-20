"""
monitor/live_monitor.py  (v2 — ETH + Base)
────────────────────────────────────────────
Subscribes to WebSocket streams for new blocks on both Ethereum and Base.
Scans every block for ERC-20 transfers from watched wallets.

WebSocket endpoints:
  Ethereum  — ALCHEMY_WS_URL       (existing env var)
  Base      — ALCHEMY_BASE_WS_URL  (new env var)

Both chains run in separate asyncio tasks sharing the same store and
signal callback. Wallet watchlist entries carry a `chain` field so
Base wallets are only matched against Base transfers.
"""

import os
import json
import asyncio
import aiohttp
from datetime import datetime

ALCHEMY_ETH_WS   = os.getenv("ALCHEMY_WS_URL",       "")
ALCHEMY_ETH_HTTP = os.getenv("ALCHEMY_HTTP_URL",      "")
ALCHEMY_BASE_WS  = os.getenv("ALCHEMY_BASE_WS_URL",   "")
ALCHEMY_BASE_HTTP= os.getenv("ALCHEMY_BASE_HTTP_URL", "")

GWEI = 1e9
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class ChainMonitor:
    """Monitors a single chain via its Alchemy WebSocket + HTTP endpoints."""

    def __init__(self, chain: str, ws_url: str, http_url: str, store, on_signal):
        self.chain     = chain
        self.ws_url    = ws_url
        self.http_url  = http_url
        self.store     = store
        self.on_signal = on_signal
        self.running   = False
        self._last_block = 0

    async def _get_logs(self, session: aiohttp.ClientSession,
                        block_hex: str) -> list[dict]:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method":  "eth_getLogs",
            "params":  [{"fromBlock": block_hex, "toBlock": block_hex,
                         "topics": [ERC20_TRANSFER_TOPIC]}],
        }
        try:
            async with session.post(self.http_url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
                return d.get("result", [])
        except Exception as e:
            print(f"[Monitor:{self.chain}] Log fetch error: {e}")
            return []

    async def _get_tx_detail(self, session: aiohttp.ClientSession,
                             tx_hash: str) -> dict:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method":  "eth_getTransactionByHash",
            "params":  [tx_hash],
        }
        try:
            async with session.post(self.http_url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=8)) as r:
                d = await r.json()
                return d.get("result") or {}
        except Exception:
            return {}

    async def _process_log(self, log: dict, session: aiohttp.ClientSession):
        try:
            if len(log.get("topics", [])) < 3:
                return

            from_addr  = ("0x" + log["topics"][1][-40:]).lower()
            to_addr    = ("0x" + log["topics"][2][-40:]).lower()
            tx_hash    = log["transactionHash"]
            block_num  = int(log["blockNumber"], 16)
            token_addr = log["address"].lower()

            watchlist = self.store.get_watchlist()
            watcher_match = watchlist.get(from_addr)
            if not watcher_match:
                return

            # Only fire if the wallet was added for this chain
            wallet_chain = watcher_match.get("chain", "ethereum")
            if wallet_chain != self.chain:
                return

            tx_detail        = await self._get_tx_detail(session, tx_hash)
            gas_limit        = int(tx_detail.get("gas", "0x0"), 16)
            max_priority_fee = int(tx_detail.get("maxPriorityFeePerGas", "0x0"), 16) / GWEI
            max_fee          = int(tx_detail.get("maxFeePerGas",         "0x0"), 16) / GWEI

            try:
                amount = int(log["data"], 16) / 1e18
            except Exception:
                amount = 0

            expected_fp    = watcher_match.get("fingerprint", {})
            confidence, score, matched = self._score_live_tx(
                gas_limit, max_priority_fee, max_fee, expected_fp, from_addr, watchlist
            )

            signal = {
                "tx_hash":          tx_hash,
                "from_address":     from_addr,
                "to_address":       to_addr,
                "token_address":    token_addr,
                "token_symbol":     watcher_match.get("symbol", "?"),
                "chain":            self.chain,
                "amount":           round(amount, 4),
                "gas_limit":        gas_limit,
                "max_priority_fee": round(max_priority_fee, 4),
                "max_fee":          round(max_fee, 4),
                "block_number":     block_num,
                "timestamp":        datetime.utcnow().isoformat(),
                "confidence":       confidence,
                "score":            score,
                "matched_rules":    matched,
                "wallet_meta":      watcher_match,
                "pattern_label":    watcher_match.get("cluster_label", "unknown"),
            }

            self.store.increment_hit(from_addr)
            self.on_signal(signal)

        except Exception as e:
            print(f"[Monitor:{self.chain}] Process log error: {e}")

    def _score_live_tx(self, gas_limit, max_priority_fee, max_fee,
                       expected_fp: dict, from_addr: str, watchlist: dict):
        score   = 0
        matched = []

        score += 40
        matched.append("watched_wallet_active")

        tol = float(os.getenv("GAS_FUZZY_TOLERANCE_PCT", 5)) / 100

        def close(a, b):
            if b == 0: return a == 0
            return abs(a - b) / b <= tol

        exp_gl  = expected_fp.get("gas_limit", 0)
        exp_mpf = expected_fp.get("max_priority_fee", 0)
        exp_mf  = expected_fp.get("max_fee", 0)

        if exp_gl and gas_limit == exp_gl:
            score += 30; matched.append(f"exact_gas_limit={exp_gl}")
        elif exp_gl and close(gas_limit, exp_gl):
            score += 15; matched.append(f"fuzzy_gas_limit≈{exp_gl}")

        if exp_mpf and max_priority_fee == exp_mpf:
            score += 15; matched.append(f"exact_priority_fee={exp_mpf}gwei")
        elif exp_mpf and close(max_priority_fee, exp_mpf):
            score += 8;  matched.append(f"fuzzy_priority_fee≈{exp_mpf}gwei")

        if exp_mf and max_fee == exp_mf:
            score += 15; matched.append(f"exact_max_fee={exp_mf}gwei")
        elif exp_mf and close(max_fee, exp_mf):
            score += 8;  matched.append(f"fuzzy_max_fee≈{exp_mf}gwei")

        score = min(score, 100)
        if score >= 80:   confidence = "high"
        elif score >= 50: confidence = "medium"
        else:             confidence = "low"

        return confidence, score, matched

    async def start(self):
        if not self.ws_url or not self.http_url:
            print(f"[Monitor:{self.chain}] No WS/HTTP URL configured — skipping")
            return

        self.running = True
        print(f"[Monitor:{self.chain}] Connecting to {self.ws_url[:40]}...")

        import websockets
        subscribe = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method":  "eth_subscribe",
            "params":  ["newHeads"],
        })

        async with aiohttp.ClientSession() as session:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20) as ws:
                    await ws.send(subscribe)
                    print(f"[Monitor:{self.chain}] ✅ Subscribed to new block heads")

                    while self.running:
                        try:
                            msg  = await asyncio.wait_for(ws.recv(), timeout=30)
                            data = json.loads(msg)

                            if "params" in data and "result" in data["params"]:
                                block_hex = data["params"]["result"].get("number")
                                if not block_hex:
                                    continue
                                block_num = int(block_hex, 16)

                                if block_num > self._last_block:
                                    self._last_block = block_num
                                    wl_size = len(self.store.get_watchlist())
                                    print(f"[Monitor:{self.chain}] Block {block_num} "
                                          f"| watching {wl_size} wallets")
                                    logs = await self._get_logs(session, block_hex)
                                    for log in logs:
                                        await self._process_log(log, session)

                        except asyncio.TimeoutError:
                            await ws.send(json.dumps(
                                {"jsonrpc": "2.0", "method": "net_version",
                                 "params": [], "id": 99}
                            ))
                        except Exception as e:
                            print(f"[Monitor:{self.chain}] Loop error: {e}")
                            break

            except Exception as e:
                print(f"[Monitor:{self.chain}] Connection error: {e}")
                self.running = False

    def stop(self):
        self.running = False


class LiveMonitor:
    """
    Aggregator that runs ChainMonitor instances for ETH and Base concurrently.
    Backward-compatible: existing code calls monitor.start() and gets both chains.
    """

    def __init__(self, store, on_signal):
        self.store     = store
        self.on_signal = on_signal
        self._monitors = []

    async def start(self):
        monitors = []

        if ALCHEMY_ETH_WS and ALCHEMY_ETH_HTTP:
            monitors.append(ChainMonitor(
                chain    = "ethereum",
                ws_url   = ALCHEMY_ETH_WS,
                http_url = ALCHEMY_ETH_HTTP,
                store    = self.store,
                on_signal= self.on_signal,
            ))

        if ALCHEMY_BASE_WS and ALCHEMY_BASE_HTTP:
            monitors.append(ChainMonitor(
                chain    = "base",
                ws_url   = ALCHEMY_BASE_WS,
                http_url = ALCHEMY_BASE_HTTP,
                store    = self.store,
                on_signal= self.on_signal,
            ))

        if not monitors:
            print("[Monitor] No chain endpoints configured — live monitor inactive")
            return

        self._monitors = monitors
        print(f"[Monitor] Starting {len(monitors)} chain monitor(s): "
              f"{[m.chain for m in monitors]}")

        # Run all chain monitors concurrently
        await asyncio.gather(*[m.start() for m in monitors])

    def stop(self):
        for m in self._monitors:
            m.stop()
