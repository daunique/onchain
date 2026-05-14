"""
bot/telegram_bot.py
────────────────────
Staged Telegram alerts modelled on the real alert format from the screenshots.

S1_HUB_BROADCAST    → urgent pre-dump warning with lead time estimate
S1_FANOUT_CONFIRMED → highest urgency, open SHORT now
CEX_DEPOSIT         → final confirmation, dump imminent
"""

import os
import requests
from datetime import datetime

MIN_ALERT = os.getenv("MIN_CONFIDENCE_TO_ALERT", "medium")
ORDER     = {"low": 0, "medium": 1, "high": 2}


def _conf_label(conf: float) -> str:
    if conf >= 0.8:  return "high"
    if conf >= 0.5:  return "medium"
    return "low"


def _sh(addr: str) -> str:
    return f"{addr[:8]}...{addr[-5:]}" if addr and len(addr) > 13 else (addr or "")


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
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            ok = r.ok
            if ok: print(f"[Bot] ✅ Alert sent")
            else:  print(f"[Bot] ❌ Failed: {r.text[:100]}")
            return ok
        except Exception as e:
            print(f"[Bot] Error: {e}")
            return False

    def send_staged_signal(self, signal) -> bool:
        conf_label = _conf_label(signal.confidence)
        if ORDER.get(conf_label, 0) < ORDER.get(MIN_ALERT, 1):
            return False
        if signal.stage == "S1_HUB_BROADCAST":
            return self._send(self._fmt_hub(signal))
        elif signal.stage == "S1_FANOUT_CONFIRMED":
            return self._send(self._fmt_fanout(signal))
        elif signal.stage == "CEX_DEPOSIT":
            return self._send(self._fmt_cex(signal))
        return self._send(self._fmt_generic(signal))

    def _fmt_hub(self, s) -> str:
        extra     = s.extra or {}
        trio_list = "\n".join(f"  `{_sh(w)}`" for w in s.trio_wallets[:5])
        watch     = "\n".join(f"- {w}" for w in extra.get("watch_for", []))
        rules     = "\n".join(f"  \u2022 {r}" for r in extra.get("matched_rules", []))
        return f"""\U0001f534 *URGENT S1\\_HUB\\_BROADCAST*
*BEARISH* | conf `{s.confidence:.2f}`

*FROM HUB* \[HUB\]
`{_sh(s.hub_address)}`

*TO TRIO* \[TRIO\]
{trio_list}

CHAIN ETH | block `{s.block_number}`

*Signal*
HUB seeded TRIO \u2014 Pre\-dump staging\.
Empirical median lead time to price trough: *12\.6h*

*Gas Fingerprint \(shared across unconnected wallets\)*
\u251c Gas Limit:    `{s.gas_fingerprint.get('gas_limit',0):,}`
\u251c Max Priority: `{s.gas_fingerprint.get('max_priority_fee',0)} Gwei`
\u2514 Max Fee:      `{s.gas_fingerprint.get('max_fee',0)} Gwei`

*Matched Rules*
{rules}

*Watch for:*
{watch}

TX `{_sh(s.tx_hash)}`""".strip()

    def _fmt_fanout(self, s) -> str:
        extra     = s.extra or {}
        elapsed   = extra.get("elapsed_s", 0)
        trio_list = "\n".join(f"  `{_sh(w)}`" for w in s.trio_wallets[:6])
        watch     = "\n".join(f"- {w}" for w in extra.get("watch_for", []))
        return f"""\U0001f534 *URGENT S1\\_FANOUT\\_CONFIRMED*
*BEARISH* | conf `{s.confidence:.2f}`

*FROM TRIO*
{trio_list}

CHAIN ETH | block `{s.block_number}`

*Signal*
S1 fan\-out confirmed: `{elapsed}s` after broadcast\.
Coordinated multi\-wallet sweep \u2014 not human behaviour\.

*Gas Fingerprint*
\u251c Gas Limit:    `{s.gas_fingerprint.get('gas_limit',0):,}`
\u251c Max Priority: `{s.gas_fingerprint.get('max_priority_fee',0)} Gwei`
\u2514 Max Fee:      `{s.gas_fingerprint.get('max_fee',0)} Gwei`

*Watch for:*
{watch}

*\u26a1 {extra.get('trade_action','Open SHORT now')}* \u2014 window `{extra.get('trade_window','0\u201312h')}`

TX `{_sh(s.tx_hash)}`""".strip()

    def _fmt_cex(self, s) -> str:
        extra = s.extra or {}
        return f"""\U0001f534 *URGENT CEX\\_DEPOSIT\\_DETECTED*
*BEARISH* | conf `{s.confidence:.2f}`

*Wallet* `{_sh(s.hub_address)}`
*CEX* {extra.get('cex_name','Unknown').upper()}

Token: `${s.token_symbol}` | Block: `{s.block_number}`

Tokens deposited to CEX\. Dump imminent\.

*\u26a1 SHORT WINDOW CLOSING*

TX `{_sh(s.tx_hash)}`""".strip()

    def _fmt_generic(self, s) -> str:
        emoji  = {"high":"\U0001f534","medium":"\U0001f7e1","low":"\U0001f7e2"}.get(_conf_label(s.confidence),"⚪")
        rules  = "\n".join(f"  \u2022 {r}" for r in (s.extra or {}).get("matched_rules",[]))
        return f"""{emoji} *SIGNAL \u2014 ${s.token_symbol}*
Stage: `{s.stage}` | conf `{s.confidence:.2f}`

From: `{_sh(s.hub_address)}`
Gas Limit: `{s.gas_fingerprint.get('gas_limit',0):,}`
Max Priority: `{s.gas_fingerprint.get('max_priority_fee',0)} Gwei`
Max Fee: `{s.gas_fingerprint.get('max_fee',0)} Gwei`

*Rules*
{rules}

TX `{_sh(s.tx_hash)}`""".strip()

    def send_scan_complete(self, tokens_flagged: int, wallets_added: int):
        self._send(f"""\U0001f50d *Scan Complete*
Tokens flagged: *{tokens_flagged}*
Wallets added: *{wallets_added}*
`{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`
Monitoring all identified wallets live\.""")

    def send_new_cluster(self, cluster: dict):
        fp = cluster.get("fingerprint", {})
        self._send(f"""\U0001f9ec *New Wallet Cluster Discovered*
Token: *${cluster.get('symbol','?')}*
Wallets: *{cluster.get('wallet_count',0)}* | Score: *{cluster.get('score',0)}/100*

Shared Fingerprint:
\u251c Gas Limit:    `{fp.get('gas_limit',0):,}`
\u251c Max Priority: `{fp.get('max_priority_fee',0)} Gwei`
\u2514 Max Fee:      `{fp.get('max_fee',0)} Gwei`

Wallets now monitored live\.""")

    def send_startup(self, watchlist_size: int, tokens_tracked: int):
        self._send(f"""\U0001f7e2 *OnChain Sentinel v2 \u2014 ONLINE*
Mode: *Fully Autonomous*
Tokens tracked: `{tokens_tracked}`
Wallets on watchlist: `{watchlist_size}`

Staged alerts active:
S1\\_HUB\\_BROADCAST \u2192 S1\\_FANOUT\\_CONFIRMED \u2192 CEX\\_DEPOSIT""")

    def send_daily_summary(self, stats: dict):
        self._send(f"""\U0001f4ca *Daily Summary*
Tokens: `{stats.get('tokens_tracked',0)}`
Clusters: `{stats.get('clusters_found',0)}`
Wallets watched: `{stats.get('wallets_watched',0)}`

Signals:
\U0001f534 High:   `{stats.get('high_signals',0)}`
\U0001f7e1 Medium: `{stats.get('med_signals',0)}`
\U0001f7e2 Low:    `{stats.get('low_signals',0)}`""")
