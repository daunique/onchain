"""
monitor/live_monitor.py
────────────────────────
Subscribes to Alchemy WebSocket for new blocks.
On each block, scans for any token transfers FROM wallets on the watchlist.
When a watched wallet moves, fires a signal with full context.
"""

import os
import json
import asyncio
import aiohttp
from datetime import datetime

ALCHEMY_WS_URL   = os.getenv("ALCHEMY_WS_URL",   "")
ALCHEMY_HTTP_URL = os.getenv("ALCHEMY_HTTP_URL",  "")
ETHERSCAN_KEY    = os.getenv("ETHERSCAN_API_KEY", "")
GWEI = 1e9

ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class LiveMonitor:
    def __init__(self, store, on_signal):
        self.store     = store
        self.on_signal = on_signal
        self.running   = False
        self._last_block = 0

    async def _get_logs(self, session: aiohttp.ClientSession, block_hex: str) -> list[dict]:
        """Get all ERC-20 Transfer logs in a block."""
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method":  "eth_getLogs",
            "params":  [{"fromBlock": block_hex, "toBlock": block_hex, "topics": [ERC20_TRANSFER_TOPIC]}],
        }
        try:
            async with session.post(ALCHEMY_HTTP_URL, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
                return d.get("result", [])
        except Exception as e:
            print(f"[Monitor] Log fetch error: {e}")
            return []

    async def _get_tx_detail(self, session: aiohttp.ClientSession, tx_hash: str) -> dict:
        """Fetch transaction details for gas info."""
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method":  "eth_getTransactionByHash",
            "params":  [tx_hash],
        }
        try:
            async with session.post(ALCHEMY_HTTP_URL, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=8)) as r:
                d = await r.json()
                return d.get("result") or {}
        except Exception:
            return {}

    async def _process_log(self, log: dict, session: aiohttp.ClientSession):
        """Check if a transfer involves a watched wallet. Fire signal if so."""
        try:
            if len(log.get("topics", [])) < 3:
                return

            from_addr  = ("0x" + log["topics"][1][-40:]).lower()
            to_addr    = ("0x" + log["topics"][2][-40:]).lower()
            tx_hash    = log["transactionHash"]
            block_num  = int(log["blockNumber"], 16)
            token_addr = log["address"].lower()

            watchlist = self.store.get_watchlist()

            # Check if from_address is on watchlist
            watcher_match = watchlist.get(from_addr)
            if not watcher_match:
                return

            # Fetch gas details
            tx_detail        = await self._get_tx_detail(session, tx_hash)
            gas_limit        = int(tx_detail.get("gas", "0x0"), 16)
            max_priority_fee = int(tx_detail.get("maxPriorityFeePerGas", "0x0"), 16) / GWEI
            max_fee          = int(tx_detail.get("maxFeePerGas", "0x0"), 16) / GWEI

            try:
                amount = int(log["data"], 16) / 1e18
            except Exception:
                amount = 0

            # Score this signal
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
            print(f"[Monitor] Process log error: {e}")

    def _score_live_tx(self, gas_limit, max_priority_fee, max_fee,
                       expected_fp: dict, from_addr: str, watchlist: dict):
        """Score a live tx against the expected fingerprint for this wallet."""
        score   = 0
        matched = []

        # Always a hit because the wallet itself is on the watchlist
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
        self.running = True
        print(f"[Monitor] Connecting to Alchemy WebSocket...")

        import websockets
        subscribe = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method":  "eth_subscribe",
            "params":  ["newHeads"],
        })

        async with aiohttp.ClientSession() as session:
            try:
                async with websockets.connect(ALCHEMY_WS_URL, ping_interval=20) as ws:
                    await ws.send(subscribe)
                    print("[Monitor] ✅ Subscribed to new block heads")

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
                                    print(f"[Monitor] Block {block_num} | watching {wl_size} wallets")
                                    logs = await self._get_logs(session, block_hex)
                                    for log in logs:
                                        await self._process_log(log, session)

                        except asyncio.TimeoutError:
                            await ws.send(json.dumps({"jsonrpc":"2.0","method":"net_version","params":[],"id":99}))
                        except Exception as e:
                            print(f"[Monitor] Loop error: {e}")
                            break

            except Exception as e:
                print(f"[Monitor] Connection error: {e}")
                self.running = False

    def stop(self):
        self.running = False
