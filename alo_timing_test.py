#!/usr/bin/env python3
"""
ALO TIMING TEST - Real market data collection
Sends 10 ALO trades and measures exact fill times for perp and spot orders.
"""

import asyncio
import time
import json
from datetime import datetime
from typing import Dict, List, Optional
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
import eth_account


# ============================================================================
# CONFIGURATION
# ============================================================================

BASE_URL = constants.MAINNET_API_URL
PERP_NAME = "HYPE"
SPOT_SYMBOL = "HYPE/USDC"

# Test parameters
NUM_TRADES = 10
TRADE_SIZE_PERP_USD = 3.0   # $3 for perp (minimum size)
TRADE_SIZE_SPOT_USD = 11.0  # $11 for spot (minimum $10 requirement)
THRESHOLD_BPS = -3.0  # Always trigger (for testing)
MAX_WAIT_TIME_MS = 10000  # 10 seconds max wait per order
CLOSE_WAIT_TIME_MS = 10000  # 10 seconds max wait for close

# Load wallet
with open('/home/ubuntu/hl_arb_project/wallet_config.json') as f:
    wallet_config = json.load(f)
    SECRET_KEY = wallet_config['api_wallet']['secret_key']

account = eth_account.Account.from_key(SECRET_KEY)
ADDRESS = account.address


# ============================================================================
# ORDER TRACKING
# ============================================================================

class OrderTracker:
    """Tracks individual order fill times."""

    def __init__(self, order_id: str, coin: str, is_buy: bool, size: float, price: float):
        self.order_id = order_id
        self.coin = coin
        self.is_buy = is_buy
        self.size = size
        self.price = price
        self.sent_time = time.time()
        self.fill_time: Optional[float] = None
        self.filled_size: float = 0.0
        self.status = "pending"  # pending, filled, partial, cancelled, timeout

    def get_fill_duration_ms(self) -> Optional[float]:
        """Returns fill duration in milliseconds."""
        if self.fill_time:
            return (self.fill_time - self.sent_time) * 1000
        return None

    def is_filled(self) -> bool:
        """Check if fully filled."""
        return abs(self.filled_size - self.size) < 0.01  # Allow small rounding error


# ============================================================================
# TEST ENGINE
# ============================================================================

class ALOTimingTest:
    """Runs ALO timing experiments and collects data."""

    def __init__(self):
        self.info = Info(BASE_URL, skip_ws=True)
        self.exchange = Exchange(account, BASE_URL)

        # Get asset info
        self.spot_coin = self.info.name_to_coin.get(SPOT_SYMBOL)
        if not self.spot_coin:
            raise RuntimeError(f"Could not resolve spot coin for {SPOT_SYMBOL}")

        self.perp_asset = self.info.name_to_asset(PERP_NAME)
        self.spot_asset = self.info.name_to_asset(self.spot_coin)

        self.perp_sz_decimals = self.info.asset_to_sz_decimals[self.perp_asset]
        self.spot_sz_decimals = self.info.asset_to_sz_decimals[self.spot_asset]

        # Results storage
        self.results: List[Dict] = []

    def _quantize_size(self, size: float, decimals: int) -> float:
        """Quantize size to exchange decimals."""
        return round(size, decimals)

    def _quantize_price(self, price: float, decimals: int) -> float:
        """Quantize price to tick size."""
        return round(price, decimals)

    async def get_current_prices(self) -> Dict:
        """Get current market prices."""
        perp_book = self.info.l2_snapshot(PERP_NAME)
        spot_book = self.info.l2_snapshot(self.spot_coin)

        perp_bid = float(perp_book['levels'][0][0]['px'])
        perp_ask = float(perp_book['levels'][0][1]['px'])
        spot_bid = float(spot_book['levels'][0][0]['px'])
        spot_ask = float(spot_book['levels'][0][1]['px'])

        return {
            'perp_bid': perp_bid,
            'perp_ask': perp_ask,
            'spot_bid': spot_bid,
            'spot_ask': spot_ask,
            'mid': (perp_bid + perp_ask + spot_bid + spot_ask) / 4
        }

    async def send_alo_order(self, coin: str, is_buy: bool, size: float, price: float) -> Optional[str]:
        """
        Send ALO (Add Liquidity Only) order.
        Returns order ID or None if failed.
        """
        order_type = {"limit": {"tif": "Alo"}}

        try:
            # Use coin directly for spot, name for perp
            result = self.exchange.order(coin, is_buy, size, price, order_type, False)

            # Parse response
            response = result.get("response", {})
            data = response.get("data", {})
            statuses = data.get("statuses", [])

            if statuses and len(statuses) > 0:
                status = statuses[0]

                # Check for error
                if status.get("error"):
                    print(f"      ❌ {coin} order rejected: {status['error']}")
                    return None

                # Get resting order info
                resting = status.get("resting", {})
                oid = resting.get("oid")

                if oid:
                    return str(oid)

            return None

        except Exception as e:
            print(f"      ❌ Exception sending {coin} order: {e}")
            return None

    async def check_order_status(self, tracker: OrderTracker) -> bool:
        """
        Check if order is filled using open orders API.
        Returns True if order no longer in open orders (i.e., filled).
        """
        try:
            # Get open orders
            open_orders_result = self.info.open_orders(ADDRESS)

            # Check if our order is still open
            order_still_open = False
            for order in open_orders_result:
                if str(order.get('oid')) == str(tracker.order_id):
                    order_still_open = True

                    # Check if partially filled
                    sz = float(order.get('sz', 0))
                    orig_sz = float(order.get('origSz', tracker.size))
                    filled = orig_sz - sz

                    if filled > 0:
                        tracker.filled_size = filled
                        if filled >= tracker.size * 0.99:  # 99% filled = consider filled
                            if tracker.status == "pending":
                                tracker.fill_time = time.time()
                                tracker.status = "filled"
                            return True
                    break

            # If order not in open orders, it's filled or cancelled
            if not order_still_open and tracker.status == "pending":
                tracker.fill_time = time.time()
                tracker.filled_size = tracker.size
                tracker.status = "filled"
                return True

            return False

        except Exception as e:
            print(f"      ⚠️  Error checking status: {e}")
            return False

    async def wait_for_fill(self, tracker: OrderTracker, timeout_ms: int = MAX_WAIT_TIME_MS) -> bool:
        """
        Wait for order to fill, checking every 100ms.
        Returns True if filled within timeout.
        """
        start = time.time()
        timeout_sec = timeout_ms / 1000

        while (time.time() - start) < timeout_sec:
            filled = await self.check_order_status(tracker)
            if filled:
                return True

            await asyncio.sleep(0.1)  # Check every 100ms

        # Timeout
        tracker.status = "timeout"
        return False

    async def cancel_order(self, coin: str, oid: int):
        """Cancel an order by ID."""
        try:
            cancel_request = {
                "coin": coin,
                "oid": oid
            }
            result = self.exchange.cancel(coin, oid)
            print(f"      🚫 Cancelled order {oid}")
        except Exception as e:
            print(f"      ⚠️  Error cancelling: {e}")

    async def close_position_ioc(self, coin: str, is_buy: bool, size: float):
        """Close a position using IOC (guaranteed fill)."""
        try:
            # Get current price
            if coin == PERP_NAME:
                book = self.info.l2_snapshot(coin)
                decimals = 5
            else:
                book = self.info.l2_snapshot(coin)
                decimals = 2

            # Aggressive pricing for IOC
            bid = float(book['levels'][0][0]['px'])
            ask = float(book['levels'][0][1]['px'])

            if is_buy:
                price = self._quantize_price(ask * 1.001, decimals)  # Buy above ask
            else:
                price = self._quantize_price(bid * 0.999, decimals)  # Sell below bid

            # Send IOC order
            order_type = {"limit": {"tif": "Ioc"}}
            result = self.exchange.order(coin, is_buy, size, price, order_type, False)

            print(f"      ✅ IOC close executed: {'BUY' if is_buy else 'SELL'} {size} @ ${price}")

        except Exception as e:
            print(f"      ❌ Error closing with IOC: {e}")

    async def run_single_trade(self, trade_num: int, direction: str) -> Dict:
        """
        Run a single ALO trade cycle: OPEN with ALO -> CLOSE with ALO
        Measures timing for both open and close operations.

        direction: "perp->spot" (short perp, buy spot) or "spot->perp" (sell spot, long perp)
        """
        print(f"\n{'='*70}")
        print(f"🧪 TEST #{trade_num} - Direction: {direction}")
        print(f"{'='*70}")

        # Get current prices
        prices = await self.get_current_prices()
        mid = prices['mid']

        # Calculate trade sizes (different for perp and spot)
        perp_size = self._quantize_size(TRADE_SIZE_PERP_USD / mid, self.perp_sz_decimals)
        spot_size = self._quantize_size(TRADE_SIZE_SPOT_USD / mid, self.spot_sz_decimals)

        print(f"  Mid price: ${mid:.4f}")
        print(f"  Perp size: {perp_size} {PERP_NAME} (${perp_size * mid:.2f})")
        print(f"  Spot size: {spot_size} {PERP_NAME} (${spot_size * mid:.2f})")
        print()

        # ========== PHASE 1: OPEN with ALO ==========
        print(f"  📤 PHASE 1: Opening position with ALO...")
        print()

        # Prepare ALO orders based on direction
        if direction == "perp->spot":
            # Short perp at bid (sell), Buy spot at bid (buy)
            open_perp_is_buy = False
            open_perp_price = self._quantize_price(prices['perp_bid'], 5)
            open_spot_is_buy = True
            open_spot_price = self._quantize_price(prices['spot_bid'], 2)
        else:
            # Long perp at ask (buy), Sell spot at ask (sell)
            open_perp_is_buy = True
            open_perp_price = self._quantize_price(prices['perp_ask'], 5)
            open_spot_is_buy = False
            open_spot_price = self._quantize_price(prices['spot_ask'], 2)

        print(f"     Perp: {'BUY' if open_perp_is_buy else 'SELL'} {perp_size} @ ${open_perp_price}")
        print(f"     Spot: {'BUY' if open_spot_is_buy else 'SELL'} {spot_size} @ ${open_spot_price}")

        # Send both orders
        open_perp_oid = await self.send_alo_order(PERP_NAME, open_perp_is_buy, perp_size, open_perp_price)
        open_perp_sent = time.time()

        open_spot_oid = await self.send_alo_order(self.spot_coin, open_spot_is_buy, spot_size, open_spot_price)
        open_spot_sent = time.time()

        if not perp_oid or not spot_oid:
            print(f"  ❌ Failed to send orders")
            return {
                'trade_num': trade_num,
                'direction': direction,
                'status': 'failed',
                'error': 'Order send failed'
            }

        print(f"  ✅ Orders sent successfully")
        print(f"     Perp OID: {perp_oid}")
        print(f"     Spot OID: {spot_oid}")
        print()

        # Create trackers
        perp_tracker = OrderTracker(perp_oid, PERP_NAME, perp_is_buy, size, perp_price)
        spot_tracker = OrderTracker(spot_oid, self.spot_coin, spot_is_buy, size, spot_price)

        perp_tracker.sent_time = perp_sent
        spot_tracker.sent_time = spot_sent

        # Wait for fills
        print(f"  ⏱️  Waiting for fills (max {MAX_WAIT_TIME_MS}ms)...")

        perp_task = asyncio.create_task(self.wait_for_fill(perp_tracker))
        spot_task = asyncio.create_task(self.wait_for_fill(spot_tracker))

        perp_filled, spot_filled = await asyncio.gather(perp_task, spot_task)

        # Report results
        print()
        print(f"  📊 RESULTS:")

        if perp_filled:
            perp_time = perp_tracker.get_fill_duration_ms()
            print(f"     ✅ Perp: FILLED in {perp_time:.0f}ms")
        else:
            print(f"     ❌ Perp: TIMEOUT (not filled in {MAX_WAIT_TIME_MS}ms)")
            await self.cancel_order(PERP_NAME, int(perp_oid))

        if spot_filled:
            spot_time = spot_tracker.get_fill_duration_ms()
            print(f"     ✅ Spot: FILLED in {spot_time:.0f}ms")
        else:
            print(f"     ❌ Spot: TIMEOUT (not filled in {MAX_WAIT_TIME_MS}ms)")
            await self.cancel_order(self.spot_coin, int(spot_oid))

        # Handle partial fills - close any open positions with IOC
        if perp_filled and not spot_filled:
            print(f"  ⚠️  PARTIAL FILL - Closing perp position with IOC...")
            await self.close_position_ioc(PERP_NAME, not perp_is_buy, size)
        elif spot_filled and not perp_filled:
            print(f"  ⚠️  PARTIAL FILL - Closing spot position with IOC...")
            await self.close_position_ioc(self.spot_coin, not spot_is_buy, size)

        # Record result
        result = {
            'trade_num': trade_num,
            'direction': direction,
            'timestamp': datetime.now().isoformat(),
            'size': size,
            'trade_value_usd': size * mid,
            'perp': {
                'oid': perp_oid,
                'is_buy': perp_is_buy,
                'price': perp_price,
                'status': perp_tracker.status,
                'fill_time_ms': perp_tracker.get_fill_duration_ms(),
                'filled': perp_filled
            },
            'spot': {
                'oid': spot_oid,
                'is_buy': spot_is_buy,
                'price': spot_price,
                'status': spot_tracker.status,
                'fill_time_ms': spot_tracker.get_fill_duration_ms(),
                'filled': spot_filled
            },
            'both_filled': perp_filled and spot_filled
        }

        return result

    async def run_all_tests(self):
        """Run all 10 tests."""
        print("="*70)
        print("ALO TIMING TEST - BAŞLIYOR")
        print("="*70)
        print(f"Toplam test: {NUM_TRADES}")
        print(f"Trade boyutu: ${TRADE_SIZE_USD}")
        print(f"Max bekleme: {MAX_WAIT_TIME_MS}ms")
        print()

        # Run 10 tests, alternating direction
        for i in range(NUM_TRADES):
            direction = "perp->spot" if i % 2 == 0 else "spot->perp"

            result = await self.run_single_trade(i + 1, direction)
            self.results.append(result)

            # Wait 5 seconds between tests
            if i < NUM_TRADES - 1:
                print(f"\n⏸️  5 saniye bekleniyor...")
                await asyncio.sleep(5)

        # Generate report
        self.generate_report()

    def generate_report(self):
        """Generate final analysis report."""
        print("\n\n")
        print("="*70)
        print("📊 FİNAL ANALİZ RAPORU")
        print("="*70)
        print()

        # Count successful fills
        perp_filled = [r for r in self.results if r.get('perp', {}).get('filled')]
        spot_filled = [r for r in self.results if r.get('spot', {}).get('filled')]
        both_filled = [r for r in self.results if r.get('both_filled')]

        print(f"Toplam test: {len(self.results)}")
        print(f"Perp doldu: {len(perp_filled)}/{len(self.results)} ({len(perp_filled)/len(self.results)*100:.0f}%)")
        print(f"Spot doldu: {len(spot_filled)}/{len(self.results)} ({len(spot_filled)/len(self.results)*100:.0f}%)")
        print(f"Her ikisi de doldu: {len(both_filled)}/{len(self.results)} ({len(both_filled)/len(self.results)*100:.0f}%)")
        print()

        # Timing statistics
        if perp_filled:
            perp_times = [r['perp']['fill_time_ms'] for r in perp_filled]
            print("PERP TIMING:")
            print(f"  Min: {min(perp_times):.0f}ms")
            print(f"  Max: {max(perp_times):.0f}ms")
            print(f"  Avg: {sum(perp_times)/len(perp_times):.0f}ms")
            print(f"  Median: {sorted(perp_times)[len(perp_times)//2]:.0f}ms")
            print()

        if spot_filled:
            spot_times = [r['spot']['fill_time_ms'] for r in spot_filled]
            print("SPOT TIMING:")
            print(f"  Min: {min(spot_times):.0f}ms")
            print(f"  Max: {max(spot_times):.0f}ms")
            print(f"  Avg: {sum(spot_times)/len(spot_times):.0f}ms")
            print(f"  Median: {sorted(spot_times)[len(spot_times)//2]:.0f}ms")
            print()

        # Timeout recommendations
        if perp_filled:
            perp_95 = sorted(perp_times)[int(len(perp_times) * 0.95)] if len(perp_times) > 0 else 0
            print(f"ÖNERİLEN PERP TIMEOUT: {perp_95:.0f}ms (P95)")

        if spot_filled:
            spot_95 = sorted(spot_times)[int(len(spot_times) * 0.95)] if len(spot_times) > 0 else 0
            print(f"ÖNERİLEN SPOT TIMEOUT: {spot_95:.0f}ms (P95)")

        print()
        print("="*70)

        # Save to file
        report_file = f"alo_timing_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, 'w') as f:
            json.dump(self.results, f, indent=2)

        print(f"📁 Detaylı sonuçlar kaydedildi: {report_file}")


# ============================================================================
# MAIN
# ============================================================================

async def main():
    test = ALOTimingTest()
    await test.run_all_tests()


if __name__ == "__main__":
    asyncio.run(main())
