"""
ALO-First Close Strategy
=========================

Implements intelligent close logic:
1. Try ALO (maker) first for better fees
2. Wait up to 15 minutes (900s) - safe because position is hedged
3. If timeout: cancel ALO, use IOC fallback
"""

import asyncio
import time
from typing import Dict, Any, List
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange


async def close_with_alo_first(
    trader,  # HyperliquidTrader instance
    info: Info,
    wallet_address: str,
    direction: str,
    size: float,
    perp_bid: float,
    perp_ask: float,
    spot_bid: float,
    spot_ask: float,
    alo_timeout_seconds: int = 900  # 15 minutes default (safe because hedged)
) -> Dict[str, Any]:
    """
    Close position: ALO first, then IOC fallback after timeout.

    Args:
        trader: HyperliquidTrader instance
        info: Info instance for checking order status
        wallet_address: User address for checking orders
        direction: Original trade direction
        size: Position size
        perp_bid, perp_ask, spot_bid, spot_ask: Current prices
        alo_timeout_seconds: How long to wait for ALO (default 900s = 15min)

    Returns:
        {
            "ok": True/False,
            "method": "alo" or "ioc",
            "alo_duration_ms": time taken if ALO filled,
            "perp": result,
            "spot": result
        }
    """
    print(f"🎯 CLOSE WITH ALO-FIRST: {direction}, size={size}, timeout={alo_timeout_seconds}s")

    # Reverse direction to close
    close_direction = "spot->perp" if direction == "perp->spot" else "perp->spot"

    # ========== STEP 1: Try ALO (Maker) ==========
    print(f"  📤 Step 1: Sending ALO orders (maker fees)...")

    alo_start_time = time.time()

    # Send ALO orders
    alo_result = await trader.execute(
        direction=close_direction,
        mm_best_bps=0,  # Close at market
        use_ioc=False,  # ALO = maker
        perp_bid=perp_bid,
        perp_ask=perp_ask,
        spot_bid=spot_bid,
        spot_ask=spot_ask,
        deadman_ms=0,
        reduce_only=True
    )

    if not alo_result.get("ok"):
        print(f"  ❌ ALO orders failed to send!")
        print(f"  🔄 Falling back to IOC immediately...")

        # Immediate IOC fallback
        ioc_result = await trader.execute(
            direction=close_direction,
            mm_best_bps=0,
            use_ioc=True,  # IOC = taker
            perp_bid=perp_bid,
            perp_ask=perp_ask,
            spot_bid=spot_bid,
            spot_ask=spot_ask,
            deadman_ms=0,
            reduce_only=True
        )

        return {
            "ok": ioc_result.get("ok"),
            "method": "ioc_fallback_immediate",
            "reason": "alo_send_failed",
            "perp": ioc_result.get("perp"),
            "spot": ioc_result.get("spot")
        }

    print(f"  ✅ ALO orders sent successfully")

    # ========== STEP 2: Wait and monitor ALO orders ==========
    print(f"  ⏱️  Step 2: Waiting up to {alo_timeout_seconds}s for ALO fill...")

    check_interval = 5  # Check every 5 seconds
    elapsed = 0

    while elapsed < alo_timeout_seconds:
        await asyncio.sleep(check_interval)
        elapsed = time.time() - alo_start_time

        # Check if position still open
        try:
            # Check open orders
            open_orders = info.open_orders(wallet_address)

            # If no open orders, position might be closed
            if not open_orders:
                alo_duration_ms = (time.time() - alo_start_time) * 1000
                print(f"  ✅ ALO FILLED! Duration: {alo_duration_ms:.0f}ms ({elapsed:.1f}s)")

                return {
                    "ok": True,
                    "method": "alo",
                    "alo_duration_ms": alo_duration_ms,
                    "alo_duration_seconds": elapsed,
                    "perp": alo_result.get("perp"),
                    "spot": alo_result.get("spot")
                }

            # Still have open orders
            print(f"  ⏳ Still waiting... ({elapsed:.0f}s / {alo_timeout_seconds}s)")

        except Exception as e:
            print(f"  ⚠️  Error checking orders: {e}")

    # ========== STEP 3: Timeout - Cancel ALO and use IOC ==========
    print(f"  ⏰ TIMEOUT! ALO did not fill in {alo_timeout_seconds}s")
    print(f"  🚫 Canceling ALO orders...")

    # Cancel all open orders
    try:
        open_orders = info.open_orders(wallet_address)
        if open_orders:
            # Create Exchange instance for cancellation
            ex = Exchange(trader._wallet, base_url=trader._base_url, meta=None, spot_meta=None)

            for order in open_orders:
                coin = order.get('coin')
                oid = order.get('oid')
                print(f"     Canceling {coin} order {oid}...")

                # Cancel the order
                try:
                    cancel_result = ex.cancel(coin, oid)
                    print(f"     ✅ Canceled: {cancel_result}")
                except Exception as cancel_exc:
                    print(f"     ❌ Cancel failed: {cancel_exc}")
        else:
            print(f"  ℹ️  No open orders to cancel")
    except Exception as e:
        print(f"  ⚠️  Error canceling orders: {e}")

    print(f"  🔄 Step 3: Sending IOC orders (guaranteed fill)...")

    # Send IOC orders
    ioc_result = await trader.execute(
        direction=close_direction,
        mm_best_bps=0,
        use_ioc=True,  # IOC = taker, guaranteed fill
        perp_bid=perp_bid,
        perp_ask=perp_ask,
        spot_bid=spot_bid,
        spot_ask=spot_ask,
        deadman_ms=0,
        reduce_only=True
    )

    if ioc_result.get("ok"):
        print(f"  ✅ IOC fallback successful!")
        return {
            "ok": True,
            "method": "ioc_fallback_timeout",
            "reason": "alo_timeout",
            "alo_wait_seconds": alo_timeout_seconds,
            "perp": ioc_result.get("perp"),
            "spot": ioc_result.get("spot")
        }
    else:
        print(f"  ❌ IOC fallback FAILED!")
        return {
            "ok": False,
            "method": "ioc_fallback_failed",
            "reason": "both_failed",
            "perp": ioc_result.get("perp"),
            "spot": ioc_result.get("spot")
        }
