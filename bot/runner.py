import asyncio
import json

import redis.asyncio as aioredis

from .config import settings
from .execution import HyperliquidTrader
from .hl_client import resolve_spot_index, ws_loop
from .strategy import Strategy
from .telegram_bot import init_telegram_bot, stop_telegram_bot
from .runtime_config import init_runtime_config, init_trading_state
from .storage_async import init_batch_writer, stop_batch_writer

redis_client = aioredis.Redis(**settings.redis_kwargs, encoding="utf-8", decode_responses=True)


async def broadcast(payload: dict):
    try:
        msg = json.dumps(payload)
        result = await redis_client.publish(settings.edges_channel, msg)
        # Uncomment for debug: print(f"📡 Published to {settings.edges_channel}, {result} subscribers")
    except Exception as e:
        print(f"❌ Broadcast error: {e}")
async def main():
    print(f"🚀 Starting HL Arbitrage Bot...")
    print(f"   Pair: {settings.pair_base}/{settings.pair_quote}")
    print(f"   Threshold: {settings.threshold_bps} bps")
    print(f"   Dry Run: {settings.dry_run}")
    print(f"   Alloc per trade: ${settings.alloc_per_trade_usd}")
    print()

    # Initialize runtime config and trading state
    # Need sync Redis client for runtime_config
    import redis
    sync_redis = redis.Redis(**settings.redis_kwargs, decode_responses=True)
    runtime_config = init_runtime_config(sync_redis)
    trading_state = init_trading_state(sync_redis)
    print(f"✓ Runtime config initialized")

    # Initialize Telegram bot
    telegram_bot = None
    if settings.telegram_token and settings.telegram_chat_id:
        try:
            telegram_bot = await init_telegram_bot(settings.telegram_token, settings.telegram_chat_id)
            print(f"✓ Telegram bot initialized")
        except Exception as e:
            print(f"⚠️  Telegram bot failed to start: {e}")
    else:
        print(f"ℹ️  Telegram bot disabled (no token/chat_id configured)")

    spot_index = await resolve_spot_index(settings.pair_base, settings.pair_quote)
    if spot_index is None:
        raise SystemExit("Could not resolve spot index for pair")
    print(f"✓ Spot index resolved: {spot_index}")

    # 🚀 PERFORMANCE: Initialize async batch writer (1s flush, 100 buffer)
    batch_writer = await init_batch_writer(batch_size=100, flush_interval=1.0)
    print(f"✓ Async batch writer initialized")

    trader = HyperliquidTrader() if not settings.dry_run else None
    if trader:
        print(f"✓ Trader initialized (live mode)")
    else:
        print(f"✓ Running in DRY_RUN mode (no real orders)")

    strategy = Strategy(spot_index, broadcast, trader=trader, deadman_ms=settings.deadman_ms)
    print(f"✓ Strategy initialized")

    print(f"🔌 Connecting to WebSocket...")
    print()

    try:
        await ws_loop(spot_index, strategy)
    finally:
        # Cleanup
        print("\n🛑 Shutting down...")

        print("   Flushing batch writer...")
        await stop_batch_writer()

        if telegram_bot:
            print("   Stopping Telegram bot...")
            await stop_telegram_bot()
if __name__ == "__main__":
    asyncio.run(main())
