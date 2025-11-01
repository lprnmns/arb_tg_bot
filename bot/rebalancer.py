"""
Auto-rebalancing module for capital management.

When one side of the capital (perp USDC, spot USDC, or spot HYPE)
runs low and causes rejected trades, this module automatically
rebalances the portfolio to maintain ~1/3 allocation in each.
"""

import asyncio
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Optional

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils.signing import get_timestamp_ms, sign_l1_action

from .config import settings


def _quantize(value: float, decimals: int) -> float:
    """Quantize to fixed decimals."""
    if decimals < 0:
        raise ValueError("decimals must be non-negative")
    quant = Decimal("1").scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN))


class CapitalRebalancer:
    """
    Manages automatic capital rebalancing between:
    - Perp USDC (margin for perp trading)
    - Spot USDC (for buying HYPE)
    - Spot HYPE (for selling on spot)
    """

    def __init__(self):
        if not settings.api_privkey:
            raise RuntimeError("API private key not configured")

        key = settings.api_privkey[2:] if settings.api_privkey.startswith("0x") else settings.api_privkey
        self._wallet = Account.from_key(bytes.fromhex(key))

        base_url = settings.hl_info_url.replace("/info", "")
        self._info = Info(base_url, skip_ws=True)
        self._exchange = Exchange(self._wallet, base_url=base_url)
        self._is_mainnet = base_url.endswith('hyperliquid.xyz')

        self._base = settings.pair_base
        self._quote = settings.pair_quote
        self._spot_symbol = f"{self._base}/{self._quote}"

        # For vault setup: API agent wallet signs txs, but balances are in master wallet
        # If master_wallet is set, query balances from there; otherwise use API wallet
        self._balance_address = settings.master_wallet if settings.master_wallet else self._wallet.address

        # Get spot asset info
        spot_coin = self._info.name_to_coin.get(self._spot_symbol)
        if spot_coin is None:
            raise RuntimeError(f"Could not resolve spot coin for {self._spot_symbol}")
        self._spot_coin = spot_coin
        self._spot_asset = self._info.name_to_asset(self._spot_coin)
        self._spot_sz_decimals = self._info.asset_to_sz_decimals[self._spot_asset]

        # Get px_decimals from spotMeta
        spot_meta = self._info.post('/info', {'type': 'spotMeta'})
        self._spot_px_decimals = 2  # Default to 2 decimals
        for universe_item in spot_meta.get('universe', []):
            if universe_item.get('name') == self._spot_symbol:
                # szDecimals from universe (typically same as px for spot)
                self._spot_px_decimals = universe_item.get('szDecimals', 2)
                break

    def get_balances(self) -> Dict[str, float]:
        """
        Fetch current balances from Hyperliquid.
        Returns:
            {
                "perp_usdc": float,
                "spot_usdc": float,
                "spot_hype": float,
                "hype_mid_price": float
            }
        """
        user_state = self._info.user_state(self._balance_address)

        # Perp USDC = withdrawable (cross margin) + isolated position margins
        perp_usdc = float(user_state.get("withdrawable", 0))

        # Add isolated position margins
        asset_positions = user_state.get("assetPositions", [])
        for asset_pos in asset_positions:
            position = asset_pos.get("position", {})
            if position:
                leverage = position.get("leverage", {})
                if leverage.get("type") == "isolated":
                    margin_used = float(position.get("marginUsed", 0))
                    perp_usdc += margin_used

        # Spot balances - use spotClearinghouseState endpoint
        spot_state = self._info.post('/info', {
            'type': 'spotClearinghouseState',
            'user': self._balance_address
        })

        spot_balances = spot_state.get("balances", [])
        spot_usdc = 0.0
        spot_hype = 0.0

        for balance in spot_balances:
            coin = balance.get("coin")
            total = float(balance.get("total", 0))
            hold = float(balance.get("hold", 0))
            available = total - hold

            if coin == self._quote:  # USDC
                spot_usdc = available
            elif coin == self._base:  # HYPE
                spot_hype = available

        # Get current HYPE mid price
        all_mids = self._info.all_mids()
        hype_mid = 0.0
        for coin, price_str in all_mids.items():
            if coin == self._base:
                hype_mid = float(price_str)
                break

        return {
            "perp_usdc": perp_usdc,
            "spot_usdc": spot_usdc,
            "spot_hype": spot_hype,
            "hype_mid_price": hype_mid,
        }

    def calculate_rebalance_actions(self, balances: Dict[str, float], min_transfer_usd: float = 5.0) -> Dict:
        """
        Calculate what transfers are needed to rebalance.

        🎯 SMART REBALANCING: perp→spot strategy
        Target: 50-50 split between Perp USDC and Spot USDC
        - 50% in Perp USDC (for shorting perp)
        - 50% in Spot USDC (for buying spot)
        - Spot HYPE converted to USDC if needed (market sell)

        Returns:
            {
                "needs_rebalance": bool,
                "sell_hype_amount": float,  # HYPE to sell for USDC
                "perp_to_spot_usdc": float,  # positive = transfer to spot, negative = from spot
                "target_perp_usdc": float,
                "target_spot_usdc": float,
            }
        """
        perp_usdc = balances["perp_usdc"]
        spot_usdc = balances["spot_usdc"]
        spot_hype = balances["spot_hype"]
        hype_price = balances["hype_mid_price"]

        # Total value in USDC (include HYPE value for total calculation)
        spot_hype_value = spot_hype * hype_price if hype_price > 0 else 0
        total_value = perp_usdc + spot_usdc + spot_hype_value

        # 🎯 Target: 50-50 split (only Perp USDC and Spot USDC matter)
        target_perp = total_value * 0.50
        target_spot = total_value * 0.50

        # 💡 SMART: If spot HYPE exists, sell it to get USDC
        sell_hype_amount = 0.0
        if spot_hype > 0.01:  # Minimum 0.01 HYPE to avoid dust
            # Sell all HYPE to maximize spot USDC
            sell_hype_amount = spot_hype
            # After selling HYPE, spot USDC will increase
            projected_spot_usdc = spot_usdc + spot_hype_value
        else:
            projected_spot_usdc = spot_usdc

        # Calculate USDC transfer after HYPE is sold
        # Current vs target
        perp_diff = perp_usdc - target_perp
        spot_usdc_diff = projected_spot_usdc - target_spot

        # Calculate USDC transfer action
        # If perp has excess, transfer to spot (positive value)
        # If spot has excess, transfer to perp (negative value)
        perp_to_spot_usdc = perp_diff

        # Check if rebalance needed
        needs_rebalance = (
            sell_hype_amount > 0.01 or  # Need to sell HYPE
            abs(perp_to_spot_usdc) > min_transfer_usd  # Need USDC transfer
        )

        return {
            "needs_rebalance": needs_rebalance,
            "total_value_usdc": total_value,
            "sell_hype_amount": sell_hype_amount,
            "perp_to_spot_usdc": perp_to_spot_usdc,
            "target_perp_usdc": target_perp,
            "target_spot_usdc": target_spot,
            "current": {
                "perp_usdc": perp_usdc,
                "spot_usdc": spot_usdc,
                "projected_spot_usdc": projected_spot_usdc,
                "spot_hype": spot_hype,
                "spot_hype_value": spot_hype_value,
            }
        }

    def execute_rebalance(self, actions: Dict, min_transfer_usd: float = 5.0) -> Dict:
        """
        Execute the rebalancing actions.

        🎯 SMART REBALANCING:
        1. Sell spot HYPE to USDC (market sell)
        2. Transfer USDC between perp and spot (50-50 split)

        Returns:
            {
                "hype_sell": response or None,
                "usdc_transfer": response or None,
            }
        """
        results = {
            "hype_sell": None,
            "usdc_transfer": None,
        }

        # STEP 1: Sell spot HYPE if needed (MARKET SELL - ALL HYPE)
        sell_hype_amount = actions.get("sell_hype_amount", 0)
        if sell_hype_amount > 0.01:
            try:
                # Quantize HYPE size to proper decimals
                hype_size = _quantize(sell_hype_amount, self._spot_sz_decimals)

                # Get current market price for aggressive sell
                all_mids = self._info.all_mids()
                hype_mid = 0.0
                for coin, price_str in all_mids.items():
                    if coin == self._base:
                        hype_mid = float(price_str)
                        break

                # Aggressive sell price: 5% below mid for INSTANT fill
                aggressive_price = hype_mid * 0.95 if hype_mid > 0 else 0.01
                # ✅ FIX: Quantize price to tick size to avoid "Price must be divisible by tick size" error
                sell_price = _quantize(aggressive_price, self._spot_px_decimals)

                print(f"💰 MARKET SELL: {hype_size} HYPE @ ${sell_price:.4f} (IOC - instant fill)")

                # Market sell: IOC with aggressive price (guaranteed instant fill)
                order_result = self._exchange.order(
                    self._spot_coin,  # coin (spot symbol)
                    False,  # is_buy (False = sell)
                    hype_size,  # sz (FULL AMOUNT - all HYPE)
                    sell_price,  # 5% below mid = instant fill
                    {"limit": {"tif": "Ioc"}},  # IOC = instant or cancel
                )

                results["hype_sell"] = order_result

                # Check if order was successful
                if isinstance(order_result, dict):
                    if order_result.get("status") == "ok":
                        # Check for errors in response data
                        response = order_result.get("response", {})
                        data = response.get("data", {})
                        statuses = data.get("statuses", [])

                        # Look for errors in statuses
                        errors = [s.get("error") for s in statuses if s.get("error")]
                        if errors:
                            print(f"❌ HYPE sell rejected: {errors[0]}")
                        else:
                            print(f"✅ HYPE market sell successful")
                    else:
                        print(f"❌ HYPE sell failed: {order_result}")
                else:
                    print(f"✅ HYPE sell order placed")

                # Wait a bit for order to settle
                import time
                time.sleep(2)

            except Exception as e:
                print(f"❌ HYPE sell error: {e}")
                results["hype_sell"] = {"error": repr(e)}
                # Continue to USDC transfer even if HYPE sell failed

        # STEP 2: Transfer USDC between perp and spot to maintain 50-50 balance
        transfer_amount = actions["perp_to_spot_usdc"]
        if abs(transfer_amount) > min_transfer_usd:
            try:
                to_perp = transfer_amount < 0
                amount = abs(transfer_amount)

                print(f"💸 Transferring ${amount:.2f} USDC: {'Spot → Perp' if to_perp else 'Perp → Spot'}")

                result = self._exchange.usd_class_transfer(amount, to_perp)
                results["usdc_transfer"] = result

                if isinstance(result, dict) and result.get("type") == "error":
                    print(f"❌ USDC transfer failed: {result}")
                else:
                    print(f"✅ USDC transfer successful")

            except Exception as e:
                print(f"❌ USDC transfer error: {e}")
                results["usdc_transfer"] = {"error": repr(e)}

        print(f"✅ Rebalance complete (50-50 USDC split)")
        return results

    def auto_rebalance(self, min_transfer_usd: float = 5.0, dry_run: bool = False) -> Dict:
        """
        Full auto-rebalance workflow:
        1. Get balances
        2. Calculate actions
        3. Execute (if not dry run)

        Returns:
            {
                "balances": {...},
                "actions": {...},
                "execution": {...} or None if dry_run
            }
        """
        print("\n🔍 Checking capital balances...")

        balances = self.get_balances()
        print(f"   Perp USDC: ${balances['perp_usdc']:.2f}")
        print(f"   Spot USDC: ${balances['spot_usdc']:.2f}")
        print(f"   Spot HYPE: {balances['spot_hype']:.4f} (${balances['spot_hype'] * balances['hype_mid_price']:.2f})")
        print(f"   HYPE Price: ${balances['hype_mid_price']:.2f}")

        actions = self.calculate_rebalance_actions(balances, min_transfer_usd)

        if not actions["needs_rebalance"]:
            print("✅ Balances are already balanced, no action needed")
            return {
                "balances": balances,
                "actions": actions,
                "execution": None,
            }

        print(f"\n⚖️  Rebalancing needed (50-50 target):")
        print(f"   Total Value: ${actions['total_value_usdc']:.2f}")
        print(f"   Target Perp: ${actions['target_perp_usdc']:.2f} (50%)")
        print(f"   Target Spot: ${actions['target_spot_usdc']:.2f} (50%)")

        # Show HYPE sell action
        if actions.get("sell_hype_amount", 0) > 0.01:
            hype_value = actions["sell_hype_amount"] * balances["hype_mid_price"]
            print(f"   💰 HYPE Sell: {actions['sell_hype_amount']:.4f} HYPE → ${hype_value:.2f} USDC")

        # Show USDC transfer action
        if abs(actions["perp_to_spot_usdc"]) > min_transfer_usd:
            direction = "Perp → Spot" if actions["perp_to_spot_usdc"] > 0 else "Spot → Perp"
            print(f"   💸 USDC Transfer: ${abs(actions['perp_to_spot_usdc']):.2f} ({direction})")

        execution = None
        if not dry_run:
            print("\n🚀 Executing rebalance...")
            execution = self.execute_rebalance(actions, min_transfer_usd)
        else:
            print("\n🧪 DRY RUN - No actual transfers/trades executed")

        return {
            "balances": balances,
            "actions": actions,
            "execution": execution,
        }


async def rebalance_capital_async(min_transfer_usd: float = 5.0, dry_run: bool = False) -> Dict:
    """
    Async wrapper for auto_rebalance.
    Use this from async code (like strategy.py).
    """
    loop = asyncio.get_event_loop()
    rebalancer = CapitalRebalancer()
    return await loop.run_in_executor(None, rebalancer.auto_rebalance, min_transfer_usd, dry_run)


def rebalance_capital_sync(min_transfer_usd: float = 5.0, dry_run: bool = False) -> Dict:
    """
    Sync wrapper for auto_rebalance.
    Use this from sync code or CLI.
    """
    rebalancer = CapitalRebalancer()
    return rebalancer.auto_rebalance(min_transfer_usd, dry_run)


if __name__ == "__main__":
    # CLI usage: python -m bot.rebalancer
    import sys
    dry_run = "--dry-run" in sys.argv or "-d" in sys.argv
    result = rebalance_capital_sync(min_transfer_usd=5.0, dry_run=dry_run)
    print("\n✅ Rebalance complete!")
    print(f"Result: {result}")
