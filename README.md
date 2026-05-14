# ◈ OnChain Sentinel v2 — Fully Autonomous

> Discovers CEX-listed tokens with pump/dump patterns, clusters the manipulator
> wallets by gas fingerprint automatically, monitors them live, and fires
> staged Telegram alerts — all with zero manual input.

---

## What It Does (End to End)

```
Every 30 min:
  DexScreener + CoinGecko trending tokens
      ↓
  CEX filter (must be on Bitget / Bybit / OKX / Binance)
      ↓
  Pump/dump pattern detection (price spike + retracement)
      ↓
  Etherscan: download full transfer history around pump windows
      ↓
  Gas fingerprint clustering (exact + fuzzy ±5%)
        → Groups unconnected wallets sharing identical gas settings
        → e.g. Gas Limit 200,000 | Max 6 Gwei | Priority 3 Gwei
      ↓
  Watchlist built automatically
      ↓

Live (every block):
  Alchemy WebSocket → scan all token transfers
      ↓
  Any watched wallet moves? → SignalEngine
      ↓
  Stage 1: S1_HUB_BROADCAST   (HUB → TRIO, conf 0.90+)
  Stage 2: S1_FANOUT_CONFIRMED (fan-out in <5 min, conf 0.95+)
  Stage 3: CEX_DEPOSIT         (TRIO → CEX deposit address)
      ↓
  Telegram alert + dashboard update
```

---

## Why CEX Filter Matters

Pump/dump schemes on CEX-listed tokens are actionable because:
- The token has real futures liquidity on Bitget/Bybit/OKX
- Manipulators long on CEX futures while distributing on-chain
- You can short the CEX futures contract when the signal fires
- Signal → trade is a direct, clean workflow

Pure DEX tokens with no CEX listing are filtered out automatically.

---

## Alert Stages (from screenshots)

### Stage 1 — `S1_HUB_BROADCAST`
```
🔴 URGENT S1_HUB_BROADCAST
BEARISH | conf 0.95

FROM HUB [HUB]
0x1234...5678

TO TRIO [TRIO]
0xabcd...ef01

Signal
HUB seeded TRIO — Pre-dump staging.
Empirical median lead time to price trough: 12.6h

Gas Fingerprint (shared across unconnected wallets)
├ Gas Limit:    200,000
├ Max Priority: 3 Gwei
└ Max Fee:      6 Gwei

Watch for:
- fan-out: TRIO to other TRIO members in 5 min
- TRIO → CEX deposits in 6-12h
```

### Stage 2 — `S1_FANOUT_CONFIRMED`
```
🔴 URGENT S1_FANOUT_CONFIRMED
BEARISH | conf 0.97

Signal
S1 fan-out confirmed: 132s after broadcast.
Coordinated multi-wallet sweep — not human behaviour.

Watch for:
- TRIO → CEX deposits in 6-12h
- open SHORT now (lead 0-12h)

⚡ Open SHORT now — window 0–12h
```

### Stage 3 — `CEX_DEPOSIT_DETECTED`
```
🔴 URGENT CEX_DEPOSIT_DETECTED
BEARISH | conf 0.98

Tokens deposited to CEX. Dump imminent.
⚡ SHORT WINDOW CLOSING
```

---

## Project Structure

```
sentinel-v2/
├── main.py                        ← Entry point
├── requirements.txt
├── .env.example                   ← Copy → .env
├── railway.toml
├── scanner/
│   ├── token_scanner.py           ← Discovers flagged tokens
│   └── cex_filter.py              ← Filters to CEX-listed only
├── analyzer/
│   ├── history.py                 ← Downloads transfer history
│   ├── clustering.py              ← Gas fingerprint clustering
│   └── analyzer.py                ← Orchestrates analysis
├── monitor/
│   ├── live_monitor.py            ← Alchemy WebSocket watcher
│   └── signal_engine.py          ← Staged signal logic
├── bot/
│   └── telegram_bot.py            ← Staged Telegram alerts
├── data/
│   └── store.py                   ← In-memory + JSON persistence
└── dashboard/
    ├── app.py                     ← Flask REST API
    └── static/index.html          ← Live dashboard UI
```

---

## Setup

### 1. Get your API keys (all free)

| Key | Where |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Message @BotFather → `/newbot` |
| `TELEGRAM_CHAT_ID` | Message @userinfobot |
| `ALCHEMY_API_KEY` | alchemy.com → new app → Ethereum Mainnet |
| `ALCHEMY_WS_URL` | Same dashboard → WebSocket URL |
| `ALCHEMY_HTTP_URL` | Same dashboard → HTTPS URL |
| `ETHERSCAN_API_KEY` | etherscan.io/apis → free account |

### 2. Configure

```bash
cp .env.example .env
# edit .env with your keys
```

### 3. Run locally

```bash
pip install -r requirements.txt
python main.py
```

Dashboard: http://localhost:5000

---

## Deploy to Railway

1. Push to GitHub
2. railway.app → New Project → Deploy from GitHub
3. Variables tab → add all `.env` values
4. Done — Railway auto-deploys, gives you a public dashboard URL

Or via CLI:
```bash
npm i -g @railway/cli
railway login && railway init && railway up
railway variables set TELEGRAM_BOT_TOKEN=xxx ...
```

---

## Tuning

All behaviour is controlled via `.env`:

| Variable | Default | Effect |
|---|---|---|
| `SCAN_INTERVAL_MINUTES` | 30 | How often to scan for new tokens |
| `MIN_PRICE_CHANGE_PCT` | 15 | Min % pump to flag as pump/dump |
| `MAX_TOKENS_TO_SCAN` | 20 | Tokens checked per cycle |
| `MIN_CLUSTER_SIZE` | 2 | Min wallets to form a cluster |
| `GAS_FUZZY_TOLERANCE_PCT` | 5 | ±% tolerance for gas matching |
| `FANOUT_WINDOW_SECONDS` | 300 | Window to confirm fan-out (5 min) |
| `MIN_CONFIDENCE_TO_ALERT` | medium | Alert threshold: low/medium/high |

---

## Total Cost

| Service | Cost |
|---|---|
| Alchemy WebSocket + RPC | Free (300M units/month) |
| Etherscan API | Free (5 calls/sec) |
| DexScreener API | Free (no key needed) |
| CoinGecko API | Free (no key needed) |
| Telegram Bot | Free |
| Railway | Free tier |
| **Total** | **$0** |
