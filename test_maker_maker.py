#!/usr/bin/env python3
"""
Test script for Maker-Maker Strategy.

Strategy:
- Threshold: 12 bps
- IOC: OFF (ALO maker for opening)
- use_maker_close: True (ALO maker for closing)
- Duration: 30 minutes

Expected:
- Lower fees: ~11 bps (5.5 open + 5.5 close)
- Higher success rate: ~70%+
- More trades at lower threshold
"""

import asyncio
import time
from datetime import datetime, timezone
import redis


async def run_maker_maker_test():
    """Run 30-minute Maker-Maker test."""

    # Connect to Redis
    r = redis.Redis(host='localhost', port=6379, decode_responses=True)

    print("\n" + "="*60)
    print("🧪 MAKER-MAKER STRATEGY TEST")
    print("="*60)
    print("Duration: 30 minutes")
    print("Parameters:")
    print("  - Threshold: 12 bps")
    print("  - IOC: OFF (ALO maker)")
    print("  - use_maker_close: True (maker for closing)")
    print("  - Expected fees: ~11 bps")
    print("="*60 + "\n")

    # Get start state
    try:
        # Check if trading is running
        state = r.get("bot:trading_state")
        if state != "running":
            print("⚠️  Trading is currently stopped. Starting trading...")
            r.set("bot:trading_state", "running")
    except Exception as e:
        print(f"❌ Redis connection error: {e}")
        print("Make sure Redis is running and accessible.")
        return

    # Set test parameters
    r.set("runtime_config:threshold_bps", "12")
    r.set("runtime_config:spike_extra_bps_for_ioc", "0")  # IOC OFF
    r.set("runtime_config:use_maker_close", "true")

    print("✅ Test parameters set:")
    print(f"   - threshold_bps = {r.get('runtime_config:threshold_bps')}")
    print(f"   - spike_extra_bps_for_ioc = {r.get('runtime_config:spike_extra_bps_for_ioc')}")
    print(f"   - use_maker_close = {r.get('runtime_config:use_maker_close')}")
    print()

    # Record start time
    start_time = time.time()
    test_duration = 30 * 60  # 30 minutes in seconds

    print(f"🕐 Test started at: {datetime.now().strftime('%H:%M:%S')}")
    print(f"🏁 Test will end at: {datetime.fromtimestamp(start_time + test_duration).strftime('%H:%M:%S')}")
    print()

    # Wait for test duration with progress updates
    elapsed = 0
    while elapsed < test_duration:
        minutes_elapsed = int(elapsed / 60)
        minutes_remaining = int((test_duration - elapsed) / 60)

        if minutes_elapsed > 0 and minutes_elapsed % 5 == 0 and elapsed % 60 < 5:
            print(f"⏱️  Test progress: {minutes_elapsed}/30 minutes ({minutes_remaining}m remaining)")

        await asyncio.sleep(5)
        elapsed = time.time() - start_time

    print("\n" + "="*60)
    print("✅ TEST COMPLETED")
    print("="*60)
    print(f"Duration: {test_duration / 60:.0f} minutes")
    print()
    print("📊 Check results with:")
    print("   docker compose exec -T db psql -U hluser -d hl_arb \\")
    print("     -c \"SELECT COUNT(*) as trades, SUM(realized_pnl) as total_pnl")
    print("         FROM positions")
    print(f"         WHERE opened_at > '{datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat()}';\"")
    print()
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(run_maker_maker_test())
