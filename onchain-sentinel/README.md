# ⬡ OnChain Sentinel

> Gas fingerprint monitor for on-chain pump & dump detection.  
> Watches for coordinated wallet activity in real-time and alerts you via Telegram.

---

## What It Does

- **Monitors** your target token for every transfer in real-time via Alchemy WebSocket
- **Fingerprints** each transaction by gas limit, max priority fee, and max fee
- **Scores** matches against your known patterns (exact + fuzzy ±5%)
- **Alerts** you instantly on Telegram when a high/medium confidence signal fires
- **Dashboards** a live web UI showing all signals, confidence scores, and gas details

---

## Project Structure

```
onchain-sentinel/
├── main.py                  ← Entry point (start here)
├── requirements.txt
├── .env.example             ← Copy to .env and fill in
├── railway.toml             ← Railway deployment config
├── engine/
│   ├── fingerprint.py       ← Pattern scoring engine
│   └── fetcher.py           ← Alchemy WebSocket + Etherscan fetcher
├── bot/
│   └── telegram_bot.py      ← Telegram alert formatter & sender
├── data/
│   └── store.py             ← In-memory signal store (shared state)
└── dashboard/
    ├── app.py               ← Flask API server
    └── static/
        └── index.html       ← Live dashboard UI
```

---

## Quick Start (Local)

### 1. Clone & install

```bash
git clone <your-repo>
cd onchain-sentinel
pip install -r requirements.txt
```

### 2. Set up environment

```bash
cp .env.example .env
```

Edit `.env` with your keys:

| Variable | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Message @BotFather on Telegram → `/newbot` |
| `TELEGRAM_CHAT_ID` | Message @userinfobot on Telegram |
| `ALCHEMY_API_KEY` | [alchemy.com](https://alchemy.com) → free account → new app |
| `ALCHEMY_WS_URL` | Same dashboard → WebSocket URL |
| `ETHERSCAN_API_KEY` | [etherscan.io/apis](https://etherscan.io/apis) → free account |
| `TOKEN_CONTRACT_ADDRESS` | The ERC-20 contract you want to monitor |

### 3. Add your known wallets

In `main.py`, add the wallet addresses you identified during analysis:

```python
KNOWN_WALLETS = [
    "0xWallet1YouFound...",
    "0xWallet2YouFound...",
    # etc
]
```

### 4. Run

```bash
python main.py
```

Dashboard available at: **http://localhost:5000**

---

## Deploy to Railway (Free)

### Option A: GitHub (recommended)

1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select your repo
4. Go to **Variables** tab → add all your `.env` values
5. Railway auto-deploys — your dashboard gets a public URL

### Option B: Railway CLI

```bash
npm install -g @railway/cli
railway login
railway init
railway up
```

Then set env vars:
```bash
railway variables set TELEGRAM_BOT_TOKEN=xxx
railway variables set TELEGRAM_CHAT_ID=xxx
railway variables set ALCHEMY_API_KEY=xxx
# ... etc
```

---

## Adding More Fingerprints

In `main.py`, extend the `FINGERPRINTS` list:

```python
FINGERPRINTS = [
    # Your original pattern
    {
        "gas_limit":             200000,
        "max_priority_fee_gwei": 3,
        "max_fee_gwei":          6,
    },
    # New pattern you discover
    {
        "gas_limit":             150000,
        "max_priority_fee_gwei": 2,
        "max_fee_gwei":          4,
    },
]
```

Each fingerprint is scored independently. The highest score wins.

---

## Confidence Scoring

| Score | Confidence | What it means |
|---|---|---|
| 80–100 | 🔴 HIGH | Exact match on all gas params or known wallet |
| 45–79 | 🟡 MEDIUM | Partial or fuzzy match |
| 1–44  | 🟢 LOW | Weak signal, monitor only |

Set `MIN_CONFIDENCE_TO_ALERT=high` in `.env` to only get Telegram alerts for strong signals.

---

## Telegram Alert Format

```
🔴 SIGNAL DETECTED — $TOKEN

Pattern: Exact gas fingerprint match
Confidence: HIGH (score: 100/100)

Transaction
├ Hash: 0x1a2b3c4d...ef
├ From: 0x1234...5678
├ To:   0xabcd...ef01
└ Amount: 1,250,000 tokens

Gas Fingerprint
├ Gas Limit:    200,000
├ Max Priority: 3 GWEI
└ Max Fee:      6 GWEI

Matched Rules
  • exact gas_limit=200000
  • exact priority_fee=3gwei
  • exact max_fee=6gwei

Block: 19,824,301
Time: 2025-03-15 14:22:11 UTC

⚡ Historical pattern → expect move within 48h
```

---

## Cost

Everything runs on free tiers:

| Service | Cost |
|---|---|
| Alchemy (WebSocket + RPC) | Free (300M compute units/month) |
| Etherscan API | Free (5 calls/sec) |
| Telegram Bot | Free |
| Railway hosting | Free tier available |
| **Total** | **$0** |

---

## Extending the System

- **Add more tokens**: duplicate the monitor loop in `main.py` with a different contract address
- **Timing patterns**: add a `last_seen_time` check per wallet to `fingerprint.py`
- **CEX deposit tracking**: add a known deposit address list and flag when wallets send there post-dump
- **Multi-chain**: duplicate the fetcher with BSC/Base/Arbitrum RPC URLs
