"""
engine/fetcher.py
─────────────────
Fetches on-chain data from Etherscan (historical) and Alchemy (live).
Converts raw blockchain data into TransactionData objects for the engine.
"""

import os
import asyncio
import aiohttp
import requests
import json
from datetime import datetime
from typing import Optional, Callable, AsyncGenerator

from engine.fingerprint import TransactionData


ETHERSCAN_BASE  = "https://api.etherscan.io/api"
GWEI            = 1e9


# ─────────────────────────────────────────────────────────────────────────────
#  Etherscan: Historical Transfer Fetcher
# ─────────────────────────────────────────────────────────────────────────────

class EtherscanFetcher:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def get_token_transfers(
        self,
        contract_address: str,
        start_block: int = 0,
        end_block: int = 99999999,
        limit: int = 1000,
    ) -> list[dict]:
        """Fetch ERC-20 token transfer events from Etherscan."""
        params = {
            "module":           "account",
            "action":           "tokentx",
            "contractaddress":  contract_address,
            "startblock":       start_block,
            "endblock":         end_block,
            "page":             1,
            "offset":           limit,
            "sort":             "desc",
            "apikey":           self.api_key,
        }
        try:
            resp = requests.get(ETHERSCAN_BASE, params=params, timeout=15)
            data = resp.json()
            if data.get("status") == "1":
                return data.get("result", [])
            print(f"[Etherscan] Warning: {data.get('message', 'Unknown error')}")
            return []
        except Exception as e:
            print(f"[Etherscan] Fetch error: {e}")
            return []

    def get_tx_detail(self, tx_hash: str) -> Optional[dict]:
        """Fetch full transaction detail (includes gas settings)."""
        params = {
            "module":  "proxy",
            "action":  "eth_getTransactionByHash",
            "txhash":  tx_hash,
            "apikey":  self.api_key,
        }
        try:
            resp = requests.get(ETHERSCAN_BASE, params=params, timeout=10)
            data = resp.json()
            return data.get("result")
        except Exception as e:
            print(f"[Etherscan] TX detail error: {e}")
            return None

    def build_transaction_data(self, transfer: dict, tx_detail: Optional[dict]) -> Optional[TransactionData]:
        """Convert Etherscan transfer + tx detail into TransactionData."""
        try:
            gas_limit        = int(tx_detail.get("gas", "0"), 16) if tx_detail else 0
            max_priority_fee = int(tx_detail.get("maxPriorityFeePerGas", "0"), 16) / GWEI if tx_detail else 0
            max_fee          = int(tx_detail.get("maxFeePerGas", "0"), 16) / GWEI if tx_detail else 0

            return TransactionData(
                tx_hash          = transfer["hash"],
                from_address     = transfer["from"],
                to_address       = transfer["to"],
                token_amount     = int(transfer["value"]) / (10 ** int(transfer.get("tokenDecimal", 18))),
                gas_limit        = gas_limit,
                max_priority_fee = max_priority_fee,
                max_fee          = max_fee,
                timestamp        = datetime.fromtimestamp(int(transfer["timeStamp"])),
                block_number     = int(transfer["blockNumber"]),
            )
        except Exception as e:
            print(f"[Fetcher] Build TX error: {e}")
            return None


# ─────────────────────────────────────────────────────────────────────────────
#  Alchemy: Real-Time Block Monitor
# ─────────────────────────────────────────────────────────────────────────────

ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class AlchemyMonitor:
    """
    Subscribes to Alchemy WebSocket for new blocks.
    On each block, scans for token transfers matching our contract.
    Calls on_signal(tx_data) when a new transfer is found.
    """

    def __init__(
        self,
        ws_url:           str,
        etherscan_key:    str,
        contract_address: str,
        on_transaction:   Callable[[TransactionData], None],
    ):
        self.ws_url           = ws_url
        self.contract_address = contract_address.lower()
        self.on_transaction   = on_transaction
        self.etherscan        = EtherscanFetcher(etherscan_key)
        self.running          = False
        self._last_block      = 0

    async def _get_logs_for_block(self, session: aiohttp.ClientSession, block_hex: str) -> list[dict]:
        """Fetch Transfer logs for the target token in a specific block."""
        payload = {
            "jsonrpc": "2.0",
            "id":      1,
            "method":  "eth_getLogs",
            "params":  [{
                "fromBlock": block_hex,
                "toBlock":   block_hex,
                "address":   self.contract_address,
                "topics":    [ERC20_TRANSFER_TOPIC],
            }],
        }
        # Convert WS URL to HTTPS for REST calls
        http_url = self.ws_url.replace("wss://", "https://").replace("ws://", "http://")
        try:
            async with session.post(http_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                return data.get("result", [])
        except Exception as e:
            print(f"[Alchemy] Log fetch error: {e}")
            return []

    async def _process_log(self, log: dict):
        """Convert a raw log into TransactionData and fire callback."""
        try:
            tx_hash    = log["transactionHash"]
            from_addr  = "0x" + log["topics"][1][-40:]
            to_addr    = "0x" + log["topics"][2][-40:]
            amount     = int(log["data"], 16) / 1e18
            block_num  = int(log["blockNumber"], 16)

            # Fetch full tx to get gas settings
            tx_detail  = self.etherscan.get_tx_detail(tx_hash)
            gas_limit        = int(tx_detail.get("gas", "0"), 16) if tx_detail else 0
            max_priority_fee = int(tx_detail.get("maxPriorityFeePerGas", "0"), 16) / GWEI if tx_detail else 0
            max_fee          = int(tx_detail.get("maxFeePerGas", "0"), 16) / GWEI if tx_detail else 0

            tx = TransactionData(
                tx_hash          = tx_hash,
                from_address     = from_addr,
                to_address       = to_addr,
                token_amount     = amount,
                gas_limit        = gas_limit,
                max_priority_fee = max_priority_fee,
                max_fee          = max_fee,
                timestamp        = datetime.utcnow(),
                block_number     = block_num,
            )
            self.on_transaction(tx)

        except Exception as e:
            print(f"[Alchemy] Process log error: {e}")

    async def start(self):
        """Main WebSocket loop — subscribes to new block headers."""
        self.running = True
        print(f"[Monitor] Connecting to Alchemy WebSocket...")

        subscribe_msg = json.dumps({
            "jsonrpc": "2.0",
            "id":      1,
            "method":  "eth_subscribe",
            "params":  ["newHeads"],
        })

        async with aiohttp.ClientSession() as session:
            try:
                import websockets
                async with websockets.connect(self.ws_url) as ws:
                    await ws.send(subscribe_msg)
                    print("[Monitor] ✅ Subscribed to new block headers")

                    while self.running:
                        try:
                            msg  = await asyncio.wait_for(ws.recv(), timeout=30)
                            data = json.loads(msg)

                            if "params" in data and "result" in data["params"]:
                                block_hex = data["params"]["result"]["number"]
                                block_num = int(block_hex, 16)

                                if block_num > self._last_block:
                                    self._last_block = block_num
                                    print(f"[Monitor] New block: {block_num}")
                                    logs = await self._get_logs_for_block(session, block_hex)
                                    for log in logs:
                                        await self._process_log(log)

                        except asyncio.TimeoutError:
                            # Send ping to keep connection alive
                            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "net_version", "params": [], "id": 2}))

            except Exception as e:
                print(f"[Monitor] WebSocket error: {e}")
                self.running = False

    def stop(self):
        self.running = False
