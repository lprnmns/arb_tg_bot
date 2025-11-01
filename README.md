# HL Arbitrage Project — IOC Spot–Perp (HYPE/USDC, 14+ bps)
One‑command startup via Docker Compose. UI shows two‑way edges with colors; floating ms overlay.

## Features
- **IOC (Immediate-or-Cancel) Orders**: 100% execution guarantee, no post-only rejections
- **Auto-Rebalancing**: Automatically redistributes capital when one side runs low
- **Position Monitoring**: Tracks and closes positions with timeout safety
- **Telegram Bot**: Full-featured bot with commands and real-time notifications
- **Real-time UI**: Web dashboard showing live edges and trade history

## Quick Start
1) Copy `.env.example` to `.env` and fill values (keys, SMTP, Telegram).
2) `docker compose up --build`

## Telegram Bot Setup

### 1. Create Your Bot
1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the instructions
3. Copy the **bot token** (looks like: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)

### 2. Get Your Chat ID
1. Search for **@userinfobot** on Telegram
2. Send it any message
3. Copy your **Chat ID** (a number like: `123456789`)

### 3. Configure .env
```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=123456789
```

### 4. Start Chatting!
Once the bot is running, send `/help` to see all available commands.

## Telegram Commands

### Monitoring
- `/status` - Bot status and uptime
- `/balance` - Current capital balances
- `/positions` - Open positions
- `/edges` - Live edge values (both directions)

### History & Analytics
- `/trades` - Last 1 hour trades
- `/trades 6` - Last 6 hours trades
- `/pnl` - Last 24h PNL summary
- `/pnl 48` - Last 48h PNL summary
- `/stats` - All-time statistics

### Control
- `/stop_trade` - Stop trading (pause)
- `/start_trade` - Resume trading
- `/rebalance` - Check and rebalance capital

### Settings Management
- `/config` - Show current settings
- `/set threshold 15` - Set threshold to 15 bps
- `/set dryrun on` - Enable dry run mode
- `/set dryrun off` - Disable dry run (LIVE trading)
- `/set ioc on` - Enable IOC mode
- `/set ioc off` - Disable IOC mode
- `/set alloc 20` - Set trade size to $20

### Notifications
The bot automatically sends notifications for:
- ✅ Successful trades
- ❌ Failed trades
- 💰 Position closures (with PNL)
- ⚖️  Auto-rebalance events
- 🛑 Bot errors/stops

## Configuration (.env)
- `THRESHOLD_BPS=14` - Minimum edge to trigger trades (14 bps covers IOC fees)
- `SPIKE_EXTRA_BPS_FOR_IOC=0` - Always use IOC (set to 0)
- `ALLOC_PER_TRADE_USD=12` - Position size per trade
- `DRY_RUN=false` - Set to true for paper trading

## Capital Management

### Check Balances
```bash
python test_rebalance.py --check
```

### Dry Run Rebalance
```bash
python test_rebalance.py --dry-run
```

### Execute Rebalance
```bash
python test_rebalance.py --execute
```

**Auto-Rebalancing**: Bot automatically rebalances on first rejected trade. Maintains 1/3 allocation:
- 1/3 Perp USDC (margin)
- 1/3 Spot USDC (for buying HYPE)
- 1/3 Spot HYPE (for selling)

## Notes
- **Fees**: IOC uses taker fees (Perp 0.045%, Spot 0.070% = 11.5 bps total)
- **Threshold**: 14 bps minimum ensures profitability after fees (14 - 11.5 = 2.5 bps net)
- **Safety**: Bot stops on 2nd consecutive error (after rebalance attempt)
- **Funding**: Hourly perp funding (1/8 of 8h rate)  
