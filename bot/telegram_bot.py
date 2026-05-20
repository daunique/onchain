"""
bot/telegram_bot.py
────────────────────
Staged Telegram alerts. All f-string expressions pre-computed as variables
to stay compatible with Python 3.11 (no backslashes inside f-strings).
"""

import os
import requests
from datetime import datetime

MIN_ALERT = os.getenv("MIN_CONFIDENCE_TO_ALERT", "medium")
ORDER     = {"low": 0, "medium": 1, "high": 2}


def _conf_label(conf: float) -> str:
    if conf >= 0.8:
        return "high"
    if conf >= 0.5:
        return "medium"
    return "low"


def _sh(addr: str) -> str:
    if addr and len(addr) > 13:
        return addr[:8] + "..." + addr[-5:]
    return addr or ""


def _bullet(items: list) -> str:
    return "\n".join("  - " + str(i) for i in items)


def _rules(items: list) -> str:
    return "\n".join("  * " + str(i) for i in items)


def _fp_lines(fp: dict) -> str:
    gl  = fp.get("gas_limit", 0)
    mpf = fp.get("max_priority_fee", 0)
    mf  = fp.get("max_fee", 0)
    return f"Gas Limit: {gl:,}\nMax Priority: {mpf} Gwei\nMax Fee: {mf} Gwei"


class TelegramAlerter:
    def __init__(self):
        self.token    = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id  = os.getenv("TELEGRAM_CHAT_ID", "")
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def _send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            print("[Bot] No credentials — skipping")
            return False
        try:
            r = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
            ok = r.ok
            if ok:
                print("[Bot] Alert sent")
            else:
                print(f"[Bot] Failed: {r.text[:100]}")
            return ok
        except Exception as e:
            print(f"[Bot] Error: {e}")
            return False

    # ── Main router ───────────────────────────────────────────────────────────

    def send_staged_signal(self, signal) -> bool:
        conf_label = _conf_label(signal.confidence)
        if ORDER.get(conf_label, 0) < ORDER.get(MIN_ALERT, 1):
            return False
        if signal.stage == "S1_HUB_BROADCAST":
            return self._send(self._fmt_hub(signal))
        if signal.stage == "S1_FANOUT_CONFIRMED":
            return self._send(self._fmt_fanout(signal))
        if signal.stage == "CEX_DEPOSIT":
            return self._send(self._fmt_cex(signal))
        return self._send(self._fmt_generic(signal))

    # ── Stage 1: HUB BROADCAST ────────────────────────────────────────────────

    def _fmt_hub(self, s) -> str:
        extra      = s.extra or {}
        conf       = s.confidence
        hub        = _sh(s.hub_address)
        tx         = _sh(s.tx_hash)
        block      = s.block_number
        trio_lines = "\n".join("  " + _sh(w) for w in s.trio_wallets[:5])
        watch      = _bullet(extra.get("watch_for", []))
        rules      = _rules(extra.get("matched_rules", []))
        fp         = _fp_lines(s.gas_fingerprint)

        return (
            f"URGENT S1_HUB_BROADCAST\n"
            f"BEARISH | conf {conf:.2f}\n\n"
            f"FROM HUB:\n{hub}\n\n"
            f"TO TRIO:\n{trio_lines}\n\n"
            f"CHAIN ETH | block {block}\n\n"
            f"Signal:\n"
            f"HUB seeded TRIO - Pre-dump staging.\n"
            f"Empirical median lead time to price trough: 12.6h\n\n"
            f"Gas Fingerprint (shared across unconnected wallets):\n{fp}\n\n"
            f"Matched Rules:\n{rules}\n\n"
            f"Watch for:\n{watch}\n\n"
            f"TX: {tx}"
        )

    # ── Stage 2: FANOUT CONFIRMED ─────────────────────────────────────────────

    def _fmt_fanout(self, s) -> str:
        extra      = s.extra or {}
        conf       = s.confidence
        elapsed    = extra.get("elapsed_s", 0)
        tx         = _sh(s.tx_hash)
        block      = s.block_number
        trio_lines = "\n".join("  " + _sh(w) for w in s.trio_wallets[:6])
        watch      = _bullet(extra.get("watch_for", []))
        action     = extra.get("trade_action", "Open SHORT now")
        window     = extra.get("trade_window", "0-12h")
        fp         = _fp_lines(s.gas_fingerprint)

        return (
            f"URGENT S1_FANOUT_CONFIRMED\n"
            f"BEARISH | conf {conf:.2f}\n\n"
            f"FROM TRIO:\n{trio_lines}\n\n"
            f"CHAIN ETH | block {block}\n\n"
            f"Signal:\n"
            f"S1 fan-out confirmed: {elapsed}s after broadcast.\n"
            f"Coordinated multi-wallet sweep - not human behaviour.\n\n"
            f"Gas Fingerprint:\n{fp}\n\n"
            f"Watch for:\n{watch}\n\n"
            f"ACTION: {action} | window {window}\n\n"
            f"TX: {tx}"
        )

    # ── Stage 3: CEX DEPOSIT ──────────────────────────────────────────────────

    def _fmt_cex(self, s) -> str:
        extra  = s.extra or {}
        conf   = s.confidence
        wallet = _sh(s.hub_address)
        cex    = extra.get("cex_name", "Unknown").upper()
        sym    = s.token_symbol
        block  = s.block_number
        tx     = _sh(s.tx_hash)

        return (
            f"URGENT CEX_DEPOSIT_DETECTED\n"
            f"BEARISH | conf {conf:.2f}\n\n"
            f"Wallet: {wallet}\n"
            f"CEX: {cex}\n\n"
            f"Token: ${sym} | Block: {block}\n\n"
            f"TRIO wallet deposited to CEX. Dump imminent.\n\n"
            f"SHORT WINDOW CLOSING\n\n"
            f"TX: {tx}"
        )

    # ── Generic fallback ──────────────────────────────────────────────────────

    def _fmt_generic(self, s) -> str:
        conf   = s.confidence
        label  = _conf_label(conf)
        emoji  = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(label, "")
        sym    = s.token_symbol
        stage  = s.stage
        hub    = _sh(s.hub_address)
        fp     = _fp_lines(s.gas_fingerprint)
        tx     = _sh(s.tx_hash)
        rules  = _rules((s.extra or {}).get("matched_rules", []))

        return (
            f"{emoji} SIGNAL - ${sym}\n"
            f"Stage: {stage} | conf {conf:.2f}\n\n"
            f"From: {hub}\n\n"
            f"Gas Fingerprint:\n{fp}\n\n"
            f"Rules:\n{rules}\n\n"
            f"TX: {tx}"
        )

    # ── System messages ───────────────────────────────────────────────────────

    def send_scan_complete(self, tokens_flagged: int, wallets_added: int):
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        self._send(
            f"Scan Complete\n"
            f"Tokens flagged: {tokens_flagged}\n"
            f"Wallets added: {wallets_added}\n"
            f"{ts}\n"
            f"Monitoring all identified wallets live."
        )

    def send_new_cluster(self, cluster: dict):
        fp      = cluster.get("fingerprint", {})
        sym     = cluster.get("symbol", "?")
        count   = cluster.get("wallet_count", 0)
        score   = cluster.get("score", 0)
        label   = cluster.get("label", "")
        fp_text = _fp_lines(fp)
        self._send(
            f"New Wallet Cluster Discovered\n"
            f"Token: ${sym}\n"
            f"Wallets: {count} | Score: {score}/100\n"
            f"Label: {label}\n\n"
            f"Shared Fingerprint:\n{fp_text}\n\n"
            f"Wallets now monitored live."
        )

    def send_startup(self, watchlist_size: int, tokens_tracked: int):
        self._send(
            f"OnChain Sentinel v2 - ONLINE\n"
            f"Mode: Fully Autonomous\n"
            f"Tokens tracked: {tokens_tracked}\n"
            f"Wallets on watchlist: {watchlist_size}\n\n"
            f"Staged alerts active:\n"
            f"S1_HUB_BROADCAST -> S1_FANOUT_CONFIRMED -> CEX_DEPOSIT"
        )

    def send_daily_summary(self, stats: dict):
        tokens   = stats.get("tokens_tracked", 0)
        clusters = stats.get("clusters_found", 0)
        wallets  = stats.get("wallets_watched", 0)
        high     = stats.get("high_signals", 0)
        med      = stats.get("med_signals", 0)
        low      = stats.get("low_signals", 0)
        self._send(
            f"Daily Summary\n\n"
            f"Tokens scanned: {tokens}\n"
            f"Clusters found: {clusters}\n"
            f"Wallets watched: {wallets}\n\n"
            f"Signals:\n"
            f"  High:   {high}\n"
            f"  Medium: {med}\n"
            f"  Low:    {low}"
        )
