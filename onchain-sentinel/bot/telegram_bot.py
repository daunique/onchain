"""
bot/telegram_bot.py
────────────────────
Sends formatted Telegram alerts when a signal is detected.
"""

import os
import asyncio
import requests
from datetime import datetime
from engine.fingerprint import SignalResult


CONFIDENCE_EMOJI = {
    "high":   "🔴",
    "medium": "🟡",
    "low":    "🟢",
}

PATTERN_LABELS = {
    "known_actor_transfer":  "Known manipulator wallet active",
    "exact_gas_fingerprint": "Exact gas fingerprint match",
    "fuzzy_gas_fingerprint": "Fuzzy gas fingerprint match",
    "no_match":              "Unclassified pattern",
}


def format_alert(signal: SignalResult, token_symbol: str) -> str:
    tx    = signal.tx
    emoji = CONFIDENCE_EMOJI.get(signal.confidence, "⚪")
    label = PATTERN_LABELS.get(signal.pattern_label, signal.pattern_label)
    rules = "\n".join(f"  • {r}" for r in signal.matched_rules)
    short_from = f"{tx.from_address[:6]}...{tx.from_address[-4:]}"
    short_to   = f"{tx.to_address[:6]}...{tx.to_address[-4:]}"
    short_hash = f"{tx.tx_hash[:10]}...{tx.tx_hash[-6:]}"

    return f"""
{emoji} *SIGNAL DETECTED — ${token_symbol}*

*Pattern:* {label}
*Confidence:* {signal.confidence.upper()} (score: {signal.score}/100)

*Transaction*
├ Hash: `{short_hash}`
├ From: `{short_from}`
├ To:   `{short_to}`
└ Amount: `{tx.token_amount:,.2f}` tokens

*Gas Fingerprint*
├ Gas Limit:      `{tx.gas_limit:,}`
├ Max Priority:   `{tx.max_priority_fee} GWEI`
└ Max Fee:        `{tx.max_fee} GWEI`

*Matched Rules*
{rules}

*Block:* `{tx.block_number}`
*Time:* `{tx.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}`

⚡ Historical pattern → expect move within 48h
""".strip()


class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id   = chat_id
        self.base_url  = f"https://api.telegram.org/bot{bot_token}"
        self.min_confidence = os.getenv("MIN_CONFIDENCE_TO_ALERT", "medium")

    def _should_alert(self, confidence: str) -> bool:
        order = {"low": 0, "medium": 1, "high": 2}
        return order.get(confidence, 0) >= order.get(self.min_confidence, 1)

    def send_signal(self, signal: SignalResult, token_symbol: str = "TOKEN") -> bool:
        if not self._should_alert(signal.confidence):
            print(f"[Bot] Skipping {signal.confidence} confidence signal (below threshold)")
            return False

        message = format_alert(signal, token_symbol)
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id":    self.chat_id,
                    "text":       message,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
            if resp.ok:
                print(f"[Bot] ✅ Alert sent — {signal.confidence} confidence")
                return True
            else:
                print(f"[Bot] ❌ Send failed: {resp.text}")
                return False
        except Exception as e:
            print(f"[Bot] Error: {e}")
            return False

    def send_startup_message(self, token_symbol: str, token_address: str):
        msg = f"""
🟢 *OnChain Sentinel ONLINE*

Monitoring `${token_symbol}`
Contract: `{token_address[:10]}...{token_address[-6:]}`

System is now watching for:
• Exact gas fingerprint matches
• Known manipulator wallet activity
• Fuzzy gas parameter patterns

You'll be alerted when signals are detected.
""".strip()
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": msg, "parse_mode": "Markdown"},
                timeout=10,
            )
            print("[Bot] Startup message sent")
        except Exception as e:
            print(f"[Bot] Startup msg error: {e}")

    def send_daily_summary(self, stats: dict, token_symbol: str):
        msg = f"""
📊 *Daily Summary — ${token_symbol}*

Signals detected today:
• 🔴 High confidence: {stats.get('high_confidence', 0)}
• 🟡 Medium confidence: {stats.get('med_confidence', 0)}
• 🟢 Low confidence: {stats.get('low_confidence', 0)}

Total signals: {stats.get('total_signals', 0)}
Wallets tracked: {stats.get('known_wallets_tracked', 0)}
Fingerprints loaded: {stats.get('fingerprints_loaded', 0)}
""".strip()
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": msg, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            print(f"[Bot] Summary error: {e}")
