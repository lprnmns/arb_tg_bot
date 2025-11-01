#!/usr/bin/env python3
"""
ALO TIMING TEST V2 - Profesyonel version
Tests BOTH open and close timing with ALO orders.
Perp: $3, Spot: $11, Each trade opens then immediately closes.
"""

import asyncio
import time
import json
import statistics
from datetime import datetime
from typing import Dict, List, Optional
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
import eth_account


# Configuration
BASE_URL = constants.MAINNET_API_URL
PERP_NAME = "HYPE"
SPOT_SYMBOL = "HYPE/USDC"

NUM_TRADES = 10
TRADE_SIZE_PERP_USD = 3.0   # $3 for perp
TRADE_SIZE_SPOT_USD = 11.0  # $11 for spot (min $10)
MAX_WAIT_MS = 10000  # 10 seconds timeout

# Load wallet from environment
import os
import sys

# Add bot directory to path to import settings
sys.path.insert(0, '/home/ubuntu/hl_arb_project')
from bot.config import settings

SECRET_KEY = settings.api_privkey
if not SECRET_KEY:
    raise RuntimeError("HL_API_AGENT_PRIVATE_KEY environment variable not set")

# Remove 0x prefix if present
if SECRET_KEY.startswith('0x'):
    SECRET_KEY = SECRET_KEY[2:]

account = eth_account.Account.from_key(bytes.fromhex(SECRET_KEY))

# Use MASTER wallet for balance checks (where capital is)
# But API wallet signs transactions
ADDRESS = settings.master_wallet if settings.master_wallet else account.address
print(f"✅ API Wallet (signing): {account.address}")
print(f"✅ Master Wallet (balance): {ADDRESS}")
print()


class ALOTimingTestV2:
    """Professional ALO timing test."""

    def __init__(self):
        self.info = Info(BASE_URL, skip_ws=True)
        self.exchange = Exchange(account, BASE_URL)

        # Asset info
        self.spot_coin = self.info.name_to_coin.get(SPOT_SYMBOL)
        if not self.spot_coin:
            raise RuntimeError(f"Could not resolve spot coin for {SPOT_SYMBOL}")

        self.results = []

    def _quantize(self, value: float, decimals: int) -> float:
        return round(value, decimals)

    async def get_prices(self):
        """Get current market prices."""
        perp_book = self.info.l2_snapshot(PERP_NAME)
        spot_book = self.info.l2_snapshot(self.spot_coin)

        return {
            'perp_bid': float(perp_book['levels'][0][0]['px']),
            'perp_ask': float(perp_book['levels'][0][1]['px']),
            'spot_bid': float(spot_book['levels'][0][0]['px']),
            'spot_ask': float(spot_book['levels'][0][1]['px']),
        }

    async def send_alo(self, coin: str, is_buy: bool, size: float, price: float) -> Optional[str]:
        """Send ALO order, return OID or None."""
        try:
            order_type = {"limit": {"tif": "Alo"}}
            result = self.exchange.order(coin, is_buy, size, price, order_type, False)

            response = result.get("response", {})
            data = response.get("data", {})
            statuses = data.get("statuses", [])

            if statuses:
                status = statuses[0]
                if status.get("error"):
                    print(f"        ❌ {coin} rejected: {status['error']}")
                    return None

                resting = status.get("resting", {})
                oid = resting.get("oid")
                if oid:
                    return str(oid)

            return None
        except Exception as e:
            print(f"        ❌ Exception: {e}")
            return None

    async def wait_for_fill(self, oid: str, timeout_ms: int) -> Optional[float]:
        """
        Wait for order to fill. Returns fill time in ms, or None if timeout.
        """
        start = time.time()
        timeout_sec = timeout_ms / 1000

        while (time.time() - start) < timeout_sec:
            try:
                open_orders = self.info.open_orders(ADDRESS)

                # Check if order still open
                still_open = any(str(o.get('oid')) == str(oid) for o in open_orders)

                if not still_open:
                    # Order filled!
                    fill_time_ms = (time.time() - start) * 1000
                    return fill_time_ms

                await asyncio.sleep(0.1)  # Check every 100ms

            except Exception as e:
                print(f"        ⚠️  Check error: {e}")
                await asyncio.sleep(0.1)

        return None  # Timeout

    async def cancel_order(self, coin: str, oid: str):
        """Cancel an order."""
        try:
            self.exchange.cancel(coin, int(oid))
            print(f"        🚫 Cancelled {coin} order {oid}")
        except Exception as e:
            print(f"        ⚠️  Cancel failed: {e}")

    async def close_with_ioc(self, coin: str, is_buy: bool, size: float):
        """Emergency close with IOC."""
        try:
            if coin == PERP_NAME:
                book = self.info.l2_snapshot(coin)
                decimals = 3  # HYPE perp uses 3 decimals for price
            else:
                book = self.info.l2_snapshot(coin)
                decimals = 3  # HYPE spot also uses 3 decimals for price

            bid = float(book['levels'][0][0]['px'])
            ask = float(book['levels'][0][1]['px'])

            price = self._quantize(ask * 1.001 if is_buy else bid * 0.999, decimals)

            order_type = {"limit": {"tif": "Ioc"}}
            self.exchange.order(coin, is_buy, size, price, order_type, False)

            print(f"        ✅ IOC close: {'BUY' if is_buy else 'SELL'} {size} @ ${price}")

        except Exception as e:
            print(f"        ❌ IOC close failed: {e}")

    async def run_single_cycle(self, cycle_num: int, direction: str) -> Dict:
        """
        Run ONE complete cycle: Open with ALO -> Close with ALO
        Measures timing for both phases.
        """
        print(f"\n{'='*70}")
        print(f"🧪 CYCLE #{cycle_num} - Direction: {direction}")
        print(f"{'='*70}")

        # Get prices
        prices = await self.get_prices()
        mid = (prices['perp_bid'] + prices['perp_ask'] + prices['spot_bid'] + prices['spot_ask']) / 4

        # Calculate sizes (HYPE has 2 decimals for both perp and spot)
        perp_size = self._quantize(TRADE_SIZE_PERP_USD / mid, 2)
        spot_size = self._quantize(TRADE_SIZE_SPOT_USD / mid, 2)

        print(f"  Mid: ${mid:.4f}")
        print(f"  Perp: {perp_size} (${perp_size * mid:.2f}) | Spot: {spot_size} (${spot_size * mid:.2f})")
        print()

        # ==== PHASE 1: OPEN with ALO ====
        print(f"  📤 PHASE 1: OPEN (ALO)")

        if direction == "perp->spot":
            # Short perp, buy spot
            # ALO = post-only, must be INSIDE spread (not cross)
            open_perp_is_buy = False
            open_perp_price = self._quantize(prices['perp_ask'] * 0.9999, 3)  # Sell just below ask (3 decimals)
            open_spot_is_buy = True
            open_spot_price = self._quantize(prices['spot_bid'] * 1.0001, 3)  # Buy just above bid (3 decimals)
        else:
            # Long perp, sell spot
            # ALO = post-only, must be INSIDE spread (not cross)
            open_perp_is_buy = True
            open_perp_price = self._quantize(prices['perp_bid'] * 1.0001, 3)  # Buy just above bid (3 decimals)
            open_spot_is_buy = False
            open_spot_price = self._quantize(prices['spot_ask'] * 0.9999, 3)  # Sell just below ask (3 decimals)

        print(f"     Perp: {'BUY' if open_perp_is_buy else 'SELL'} {perp_size} @ ${open_perp_price}")
        print(f"     Spot: {'BUY' if open_spot_is_buy else 'SELL'} {spot_size} @ ${open_spot_price}")

        # Send open orders
        open_perp_oid = await self.send_alo(PERP_NAME, open_perp_is_buy, perp_size, open_perp_price)
        open_spot_oid = await self.send_alo(self.spot_coin, open_spot_is_buy, spot_size, open_spot_price)

        if not open_perp_oid or not open_spot_oid:
            print(f"  ❌ FAILED to send open orders")
            return {'cycle': cycle_num, 'status': 'failed', 'phase': 'open_send'}

        print(f"     ✅ Sent: perp_oid={open_perp_oid}, spot_oid={open_spot_oid}")
        print(f"     ⏱️  Waiting for fills (max {MAX_WAIT_MS}ms)...")

        # Wait for fills
        open_perp_task = asyncio.create_task(self.wait_for_fill(open_perp_oid, MAX_WAIT_MS))
        open_spot_task = asyncio.create_task(self.wait_for_fill(open_spot_oid, MAX_WAIT_MS))

        open_perp_time, open_spot_time = await asyncio.gather(open_perp_task, open_spot_task)

        # Check results
        open_perp_filled = open_perp_time is not None
        open_spot_filled = open_spot_time is not None

        print(f"     📊 Perp: {'✅ ' + str(int(open_perp_time)) + 'ms' if open_perp_filled else '❌ TIMEOUT'}")
        print(f"     📊 Spot: {'✅ ' + str(int(open_spot_time)) + 'ms' if open_spot_filled else '❌ TIMEOUT'}")

        # Handle failures
        if not open_perp_filled or not open_spot_filled:
            print(f"  ⚠️  PARTIAL/NO FILL - Cleaning up...")

            if not open_perp_filled:
                await self.cancel_order(PERP_NAME, open_perp_oid)
            if not open_spot_filled:
                await self.cancel_order(self.spot_coin, open_spot_oid)

            # Close any filled positions with IOC
            if open_perp_filled:
                await self.close_with_ioc(PERP_NAME, not open_perp_is_buy, perp_size)
            if open_spot_filled:
                await self.close_with_ioc(self.spot_coin, not open_spot_is_buy, spot_size)

            return {
                'cycle': cycle_num,
                'direction': direction,
                'status': 'open_failed',
                'open_perp_time_ms': open_perp_time,
                'open_spot_time_ms': open_spot_time,
            }

        print(f"  ✅ BOTH FILLED - Proceeding to close")
        print()

        # ==== PHASE 2: CLOSE with ALO ====
        print(f"  📥 PHASE 2: CLOSE (ALO)")

        # Wait 1 second before closing
        await asyncio.sleep(1)

        # Get fresh prices
        close_prices = await self.get_prices()

        # Close = reverse direction (ALO = inside spread)
        if direction == "perp->spot":
            # Close: Buy perp, sell spot
            close_perp_is_buy = True
            close_perp_price = self._quantize(close_prices['perp_bid'] * 1.0001, 3)  # Buy just above bid (3 decimals)
            close_spot_is_buy = False
            close_spot_price = self._quantize(close_prices['spot_ask'] * 0.9999, 3)  # Sell just below ask (3 decimals)
        else:
            # Close: Sell perp, buy spot
            close_perp_is_buy = False
            close_perp_price = self._quantize(close_prices['perp_ask'] * 0.9999, 3)  # Sell just below ask (3 decimals)
            close_spot_is_buy = True
            close_spot_price = self._quantize(close_prices['spot_bid'] * 1.0001, 3)  # Buy just above bid (3 decimals)

        print(f"     Perp: {'BUY' if close_perp_is_buy else 'SELL'} {perp_size} @ ${close_perp_price}")
        print(f"     Spot: {'BUY' if close_spot_is_buy else 'SELL'} {spot_size} @ ${close_spot_price}")

        # Send close orders
        close_perp_oid = await self.send_alo(PERP_NAME, close_perp_is_buy, perp_size, close_perp_price)
        close_spot_oid = await self.send_alo(self.spot_coin, close_spot_is_buy, spot_size, close_spot_price)

        if not close_perp_oid or not close_spot_oid:
            print(f"  ❌ FAILED to send close orders - Using IOC fallback")
            # Emergency close with IOC
            if not close_perp_oid:
                await self.close_with_ioc(PERP_NAME, close_perp_is_buy, perp_size)
            if not close_spot_oid:
                await self.close_with_ioc(self.spot_coin, close_spot_is_buy, spot_size)

            return {
                'cycle': cycle_num,
                'direction': direction,
                'status': 'close_send_failed',
                'open_perp_time_ms': open_perp_time,
                'open_spot_time_ms': open_spot_time,
            }

        print(f"     ✅ Sent: perp_oid={close_perp_oid}, spot_oid={close_spot_oid}")
        print(f"     ⏱️  Waiting for fills (max {MAX_WAIT_MS}ms)...")

        # Wait for close fills
        close_perp_task = asyncio.create_task(self.wait_for_fill(close_perp_oid, MAX_WAIT_MS))
        close_spot_task = asyncio.create_task(self.wait_for_fill(close_spot_oid, MAX_WAIT_MS))

        close_perp_time, close_spot_time = await asyncio.gather(close_perp_task, close_spot_task)

        # Check results
        close_perp_filled = close_perp_time is not None
        close_spot_filled = close_spot_time is not None

        print(f"     📊 Perp: {'✅ ' + str(int(close_perp_time)) + 'ms' if close_perp_filled else '❌ TIMEOUT'}")
        print(f"     📊 Spot: {'✅ ' + str(int(close_spot_time)) + 'ms' if close_spot_filled else '❌ TIMEOUT'}")

        # Handle close failures with IOC
        if not close_perp_filled or not close_spot_filled:
            print(f"  ⚠️  CLOSE TIMEOUT - Using IOC fallback")

            if not close_perp_filled:
                await self.cancel_order(PERP_NAME, close_perp_oid)
                await self.close_with_ioc(PERP_NAME, close_perp_is_buy, perp_size)

            if not close_spot_filled:
                await self.cancel_order(self.spot_coin, close_spot_oid)
                await self.close_with_ioc(self.spot_coin, close_spot_is_buy, spot_size)

        print(f"  ✅ CYCLE COMPLETE")

        # Return results
        return {
            'cycle': cycle_num,
            'direction': direction,
            'timestamp': datetime.now().isoformat(),
            'status': 'completed',
            'sizes': {'perp': perp_size, 'spot': spot_size},
            'open': {
                'perp_time_ms': open_perp_time,
                'spot_time_ms': open_spot_time,
                'both_filled': True,
            },
            'close': {
                'perp_time_ms': close_perp_time,
                'spot_time_ms': close_spot_time,
                'both_filled': close_perp_filled and close_spot_filled,
            }
        }

    async def run_all_tests(self):
        """Run all 10 test cycles."""
        print("="*70)
        print("🚀 ALO TIMING TEST V2 - PROFESSIONAL EDITION")
        print("="*70)
        print(f"Cycles: {NUM_TRADES}")
        print(f"Perp size: ${TRADE_SIZE_PERP_USD} | Spot size: ${TRADE_SIZE_SPOT_USD}")
        print(f"Timeout: {MAX_WAIT_MS}ms")
        print()

        for i in range(NUM_TRADES):
            direction = "perp->spot" if i % 2 == 0 else "spot->perp"

            result = await self.run_single_cycle(i + 1, direction)
            self.results.append(result)

            # Wait 3 seconds between cycles
            if i < NUM_TRADES - 1:
                print(f"\n⏸️  3 seconds break...")
                await asyncio.sleep(3)

        # Generate report
        self.generate_report()

    def generate_report(self):
        """Generate comprehensive analysis report."""
        print("\n\n")
        print("="*70)
        print("📊 FINAL ANALYSIS REPORT")
        print("="*70)
        print()

        completed = [r for r in self.results if r.get('status') == 'completed']

        if not completed:
            print("❌ No successful cycles completed!")
            return

        print(f"✅ Completed cycles: {len(completed)}/{len(self.results)}")
        print()

        # Extract timing data
        open_perp_times = [r['open']['perp_time_ms'] for r in completed if r['open']['perp_time_ms']]
        open_spot_times = [r['open']['spot_time_ms'] for r in completed if r['open']['spot_time_ms']]
        close_perp_times = [r['close']['perp_time_ms'] for r in completed if r['close']['perp_time_ms']]
        close_spot_times = [r['close']['spot_time_ms'] for r in completed if r['close']['spot_time_ms']]

        # OPEN TIMING
        if open_perp_times:
            print("📈 OPEN TIMING - PERP:")
            print(f"   Count:  {len(open_perp_times)}")
            print(f"   Min:    {min(open_perp_times):.0f}ms")
            print(f"   Max:    {max(open_perp_times):.0f}ms")
            print(f"   Avg:    {statistics.mean(open_perp_times):.0f}ms")
            print(f"   Median: {statistics.median(open_perp_times):.0f}ms")
            if len(open_perp_times) > 1:
                print(f"   Stdev:  {statistics.stdev(open_perp_times):.0f}ms")
            p95 = sorted(open_perp_times)[int(len(open_perp_times) * 0.95)] if len(open_perp_times) > 1 else open_perp_times[0]
            print(f"   P95:    {p95:.0f}ms ⭐ RECOMMENDED TIMEOUT")
            print()

        if open_spot_times:
            print("📈 OPEN TIMING - SPOT:")
            print(f"   Count:  {len(open_spot_times)}")
            print(f"   Min:    {min(open_spot_times):.0f}ms")
            print(f"   Max:    {max(open_spot_times):.0f}ms")
            print(f"   Avg:    {statistics.mean(open_spot_times):.0f}ms")
            print(f"   Median: {statistics.median(open_spot_times):.0f}ms")
            if len(open_spot_times) > 1:
                print(f"   Stdev:  {statistics.stdev(open_spot_times):.0f}ms")
            p95 = sorted(open_spot_times)[int(len(open_spot_times) * 0.95)] if len(open_spot_times) > 1 else open_spot_times[0]
            print(f"   P95:    {p95:.0f}ms ⭐ RECOMMENDED TIMEOUT")
            print()

        # CLOSE TIMING
        if close_perp_times:
            print("📉 CLOSE TIMING - PERP:")
            print(f"   Count:  {len(close_perp_times)}")
            print(f"   Min:    {min(close_perp_times):.0f}ms")
            print(f"   Max:    {max(close_perp_times):.0f}ms")
            print(f"   Avg:    {statistics.mean(close_perp_times):.0f}ms")
            print(f"   Median: {statistics.median(close_perp_times):.0f}ms")
            if len(close_perp_times) > 1:
                print(f"   Stdev:  {statistics.stdev(close_perp_times):.0f}ms")
            p95 = sorted(close_perp_times)[int(len(close_perp_times) * 0.95)] if len(close_perp_times) > 1 else close_perp_times[0]
            print(f"   P95:    {p95:.0f}ms ⭐ RECOMMENDED TIMEOUT")
            print()

        if close_spot_times:
            print("📉 CLOSE TIMING - SPOT:")
            print(f"   Count:  {len(close_spot_times)}")
            print(f"   Min:    {min(close_spot_times):.0f}ms")
            print(f"   Max:    {max(close_spot_times):.0f}ms")
            print(f"   Avg:    {statistics.mean(close_spot_times):.0f}ms")
            print(f"   Median: {statistics.median(close_spot_times):.0f}ms")
            if len(close_spot_times) > 1:
                print(f"   Stdev:  {statistics.stdev(close_spot_times):.0f}ms")
            p95 = sorted(close_spot_times)[int(len(close_spot_times) * 0.95)] if len(close_spot_times) > 1 else close_spot_times[0]
            print(f"   P95:    {p95:.0f}ms ⭐ RECOMMENDED TIMEOUT")
            print()

        # Success rates
        open_both = sum(1 for r in completed if r['open']['both_filled'])
        close_both = sum(1 for r in completed if r['close']['both_filled'])

        print("🎯 SUCCESS RATES:")
        print(f"   Open both filled:  {open_both}/{len(completed)} ({open_both/len(completed)*100:.0f}%)")
        print(f"   Close both filled: {close_both}/{len(completed)} ({close_both/len(completed)*100:.0f}%)")
        print()

        print("="*70)

        # Save results
        filename = f"alo_timing_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w') as f:
            json.dump(self.results, f, indent=2)

        print(f"💾 Results saved: {filename}")


async def main():
    test = ALOTimingTestV2()
    await test.run_all_tests()


if __name__ == "__main__":
    asyncio.run(main())
