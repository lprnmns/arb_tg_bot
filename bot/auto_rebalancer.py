"""
Auto-Rebalancer Service

Monitors capital distribution every 5 seconds and automatically rebalances
when imbalance exceeds 20%.

This ensures the bot always has sufficient capital on both sides to execute trades.
"""

import asyncio
import time
from typing import Optional

from .config import settings
from .rebalancer import CapitalRebalancer, rebalance_capital_async
from .telegram_bot import get_telegram_notifier


class AutoRebalancerService:
    """
    Background service that monitors capital balance and triggers rebalancing.
    """

    def __init__(self, check_interval_seconds: float = 5.0, imbalance_threshold_pct: float = 20.0):
        """
        Args:
            check_interval_seconds: How often to check balances (default: 5s)
            imbalance_threshold_pct: Trigger rebalance if imbalance > this % (default: 20%)
        """
        self.check_interval = check_interval_seconds
        self.imbalance_threshold = imbalance_threshold_pct
        self.rebalancer: Optional[CapitalRebalancer] = None
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.last_rebalance_time = 0
        self.rebalance_cooldown = 60  # Minimum 60s between rebalances
        self.rebalance_count = 0
        self.successful_rebalances = 0

    async def start(self):
        """Start the auto-rebalancer background task."""
        if self.running:
            print("⚠️ Auto-rebalancer already running")
            return

        print(f"🔄 Starting auto-rebalancer (check every {self.check_interval}s, threshold: {self.imbalance_threshold}%)")

        self.running = True
        self.task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """Stop the auto-rebalancer."""
        if not self.running:
            return

        print("🛑 Stopping auto-rebalancer...")
        self.running = False

        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self):
        """Main monitoring loop - runs continuously."""
        try:
            # Initialize rebalancer
            self.rebalancer = await asyncio.to_thread(CapitalRebalancer)

            while self.running:
                try:
                    await self._check_and_rebalance()
                except Exception as e:
                    print(f"❌ Error in rebalancer loop: {e}")
                    # Continue running even if one check fails

                # Wait for next check
                await asyncio.sleep(self.check_interval)

        except asyncio.CancelledError:
            print("✅ Auto-rebalancer stopped cleanly")
        except Exception as e:
            print(f"❌ Fatal error in auto-rebalancer: {e}")
            self.running = False

    async def _check_and_rebalance(self):
        """Check balance and rebalance if needed."""
        if not self.rebalancer:
            return

        # Fetch balances (in thread to not block)
        try:
            balances = await asyncio.to_thread(self.rebalancer.get_balances)
        except Exception as e:
            print(f"⚠️ Failed to fetch balances: {e}")
            return

        # Calculate imbalance
        # 🎯 SINGLE DIRECTION: Only check Perp USDC vs Spot USDC (50-50)
        perp_usdc = balances['perp_usdc']
        spot_usdc = balances['spot_usdc']
        spot_hype_value = balances['spot_hype'] * balances['hype_mid_price']
        total_value = perp_usdc + spot_usdc + spot_hype_value

        if total_value < 10:  # Skip if portfolio too small
            return

        # Calculate how far each bucket deviates from 50%
        target_perp = total_value * 0.50
        target_spot = total_value * 0.50

        perp_deviation = abs(perp_usdc - target_perp) / total_value * 100
        spot_usdc_deviation = abs(spot_usdc - target_spot) / total_value * 100

        max_deviation = max(perp_deviation, spot_usdc_deviation)

        # Check if rebalance needed
        if max_deviation > self.imbalance_threshold:
            # Check cooldown
            now = time.time()
            if (now - self.last_rebalance_time) < self.rebalance_cooldown:
                remaining = self.rebalance_cooldown - (now - self.last_rebalance_time)
                print(f"⏳ Rebalance needed but in cooldown ({remaining:.0f}s remaining)")
                return

            print(f"\n⚖️ AUTO-REBALANCE TRIGGERED (50-50 target)")
            print(f"   Max deviation: {max_deviation:.1f}% (threshold: {self.imbalance_threshold}%)")
            print(f"   Perp USDC: ${perp_usdc:.2f} ({perp_deviation:.1f}% deviation)")
            print(f"   Spot USDC: ${spot_usdc:.2f} ({spot_usdc_deviation:.1f}% deviation)")
            print(f"   Spot HYPE: ${spot_hype_value:.2f} (not used for perp→spot)")
            print(f"   Target: ${target_perp:.2f} each (50-50 split)")

            # Notify via Telegram
            telegram = get_telegram_notifier()
            if telegram:
                await telegram.notify_error(
                    "Auto-Rebalance Triggered",
                    f"Imbalance detected: {max_deviation:.1f}%\n\n"
                    f"Perp: ${perp_usdc:.2f}\n"
                    f"Spot USDC: ${spot_usdc:.2f}\n"
                    f"Spot HYPE: ${spot_hype_value:.2f}\n\n"
                    f"Rebalancing now..."
                )

            # Execute rebalance
            try:
                dry_run = settings.dry_run

                # Track metrics
                self.rebalance_count += 1
                deviation_before = max_deviation

                # Calculate dynamic min_transfer based on portfolio size
                # For $50 portfolio: $5, for $100: $10, for $200: $15, etc.
                dynamic_min_transfer = max(5.0, min(15.0, total_value * 0.10))
                print(f"   Using min_transfer: ${dynamic_min_transfer:.2f} ({dynamic_min_transfer/total_value*100:.1f}% of portfolio)")

                # Execute with dynamic min_transfer to avoid tiny rebalances
                result = await rebalance_capital_async(min_transfer_usd=dynamic_min_transfer, dry_run=dry_run)

                if result.get("execution"):
                    print(f"✅ Auto-rebalance executed!")
                    self.last_rebalance_time = now

                    # Wait for blockchain to process (3 seconds)
                    await asyncio.sleep(3)

                    # Verify rebalance success - check if deviation actually decreased
                    try:
                        new_balances = await asyncio.to_thread(self.rebalancer.get_balances)
                        new_perp = new_balances['perp_usdc']
                        new_spot_usdc = new_balances['spot_usdc']
                        new_spot_hype_value = new_balances['spot_hype'] * new_balances['hype_mid_price']
                        new_total = new_perp + new_spot_usdc + new_spot_hype_value

                        if new_total < 10:
                            print(f"⚠️ Portfolio too small after rebalance")
                            return

                        # 🎯 Check 50-50 split
                        new_target_perp = new_total * 0.50
                        new_target_spot = new_total * 0.50
                        new_perp_dev = abs(new_perp - new_target_perp) / new_total * 100
                        new_spot_usdc_dev = abs(new_spot_usdc - new_target_spot) / new_total * 100
                        deviation_after = max(new_perp_dev, new_spot_usdc_dev)

                        improvement = deviation_before - deviation_after

                        if improvement > 5:
                            # Rebalance was successful
                            self.successful_rebalances += 1
                            success_rate = (self.successful_rebalances / self.rebalance_count) * 100

                            print(f"✅ Rebalance verified successful!")
                            print(f"   Deviation: {deviation_before:.1f}% → {deviation_after:.1f}% (improved {improvement:.1f}%)")
                            print(f"   Success rate: {self.successful_rebalances}/{self.rebalance_count} ({success_rate:.0f}%)")

                            if telegram:
                                await telegram.notify_rebalance(
                                    True,
                                    f"Rebalance successful\n"
                                    f"Deviation: {deviation_before:.1f}% → {deviation_after:.1f}%"
                                )
                        else:
                            # Rebalance didn't help much - increase cooldown temporarily
                            print(f"⚠️ Rebalance had minimal effect")
                            print(f"   Deviation: {deviation_before:.1f}% → {deviation_after:.1f}% (improved {improvement:.1f}%)")
                            print(f"   This might indicate partial fills or timing issues")
                            print(f"   Increasing cooldown to 120s for this cycle")

                            # Temporarily extend cooldown to avoid loop
                            self.last_rebalance_time = now + 60  # Extra 60s cooldown

                    except Exception as verify_error:
                        print(f"⚠️ Could not verify rebalance: {verify_error}")
                else:
                    print(f"ℹ️ No rebalance actions needed")

            except Exception as e:
                print(f"❌ Auto-rebalance failed: {e}")

                if telegram:
                    await telegram.notify_rebalance(False, f"Auto-rebalance failed: {e}")


# Global singleton instance
_auto_rebalancer_instance: Optional[AutoRebalancerService] = None


def get_auto_rebalancer() -> Optional[AutoRebalancerService]:
    """Get the global auto-rebalancer instance."""
    return _auto_rebalancer_instance


async def init_auto_rebalancer(
    check_interval: float = 5.0,
    imbalance_threshold: float = 20.0
) -> AutoRebalancerService:
    """
    Initialize and start the auto-rebalancer service.

    Returns:
        The started AutoRebalancerService instance
    """
    global _auto_rebalancer_instance

    if _auto_rebalancer_instance is not None:
        print("⚠️ Auto-rebalancer already initialized")
        return _auto_rebalancer_instance

    service = AutoRebalancerService(
        check_interval_seconds=check_interval,
        imbalance_threshold_pct=imbalance_threshold
    )

    await service.start()

    _auto_rebalancer_instance = service
    return service
