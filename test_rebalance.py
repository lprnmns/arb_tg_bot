#!/usr/bin/env python3
"""
Test script to manually check balances and perform rebalancing.

Usage:
    python test_rebalance.py --check      # Just check balances
    python test_rebalance.py --dry-run    # Calculate rebalance actions (no execution)
    python test_rebalance.py --execute    # Actually execute rebalance
"""

import sys
import argparse
from bot.rebalancer import rebalance_capital_sync


def main():
    parser = argparse.ArgumentParser(description="Test capital rebalancing")
    parser.add_argument("--check", action="store_true", help="Only check current balances")
    parser.add_argument("--dry-run", action="store_true", help="Calculate rebalance without executing")
    parser.add_argument("--execute", action="store_true", help="Execute rebalance")
    parser.add_argument("--min-transfer", type=float, default=5.0, help="Minimum transfer amount in USD (default: 5.0)")

    args = parser.parse_args()

    if not any([args.check, args.dry_run, args.execute]):
        print("❌ Please specify an action: --check, --dry-run, or --execute")
        parser.print_help()
        sys.exit(1)

    try:
        from bot.rebalancer import CapitalRebalancer

        rebalancer = CapitalRebalancer()

        if args.check:
            print("🔍 Checking current balances...\n")
            balances = rebalancer.get_balances()

            print(f"📊 Current Balances:")
            print(f"   Perp USDC: ${balances['perp_usdc']:.2f}")
            print(f"   Spot USDC: ${balances['spot_usdc']:.2f}")
            print(f"   Spot {rebalancer._base}: {balances['spot_hype']:.4f}")
            print(f"   {rebalancer._base} Price: ${balances['hype_mid_price']:.2f}")
            print(f"   Spot {rebalancer._base} Value: ${balances['spot_hype'] * balances['hype_mid_price']:.2f}")

            total = balances['perp_usdc'] + balances['spot_usdc'] + (balances['spot_hype'] * balances['hype_mid_price'])
            print(f"\n💰 Total Portfolio Value: ${total:.2f}")

            # Check balance
            actions = rebalancer.calculate_rebalance_actions(balances, args.min_transfer)

            if actions["needs_rebalance"]:
                print(f"\n⚠️  Portfolio is IMBALANCED")
                print(f"   Target per bucket: ${actions['target_per_bucket']:.2f}")
                print(f"\n   Current distribution:")
                print(f"   - Perp USDC: ${actions['current']['perp_usdc']:.2f} (diff: ${actions['current']['perp_usdc'] - actions['target_per_bucket']:.2f})")
                print(f"   - Spot USDC: ${actions['current']['spot_usdc']:.2f} (diff: ${actions['current']['spot_usdc'] - actions['target_per_bucket']:.2f})")
                print(f"   - Spot {rebalancer._base}: ${actions['current']['spot_hype_value']:.2f} (diff: ${actions['current']['spot_hype_value'] - actions['target_per_bucket']:.2f})")

                print(f"\n   Suggested actions:")
                if abs(actions['perp_to_spot_usdc']) > args.min_transfer:
                    direction = "Perp → Spot" if actions['perp_to_spot_usdc'] > 0 else "Spot → Perp"
                    print(f"   - Transfer ${abs(actions['perp_to_spot_usdc']):.2f} USDC ({direction})")
                if abs(actions['spot_buy_hype_usdc']) > args.min_transfer:
                    action = "Buy" if actions['spot_buy_hype_usdc'] > 0 else "Sell"
                    amount = abs(actions['spot_buy_hype_usdc']) / balances['hype_mid_price']
                    print(f"   - {action} {amount:.4f} {rebalancer._base} (${abs(actions['spot_buy_hype_usdc']):.2f})")
            else:
                print(f"\n✅ Portfolio is BALANCED (within ${args.min_transfer:.2f} tolerance)")
                print(f"   Target per bucket: ${actions['target_per_bucket']:.2f}")
                print(f"   All buckets are within acceptable range")

        elif args.dry_run:
            print("🧪 DRY RUN: Calculating rebalance actions...\n")
            result = rebalance_capital_sync(min_transfer_usd=args.min_transfer, dry_run=True)
            print("\n✅ Dry run complete. No actual transfers were made.")

        elif args.execute:
            print("🚀 EXECUTING REBALANCE...\n")
            print("⚠️  WARNING: This will perform REAL transfers and trades!")

            confirm = input("Type 'yes' to confirm: ")
            if confirm.lower() != 'yes':
                print("❌ Cancelled")
                sys.exit(0)

            result = rebalance_capital_sync(min_transfer_usd=args.min_transfer, dry_run=False)
            print("\n✅ Rebalance complete!")

            if result.get("execution"):
                exec_result = result["execution"]
                if exec_result.get("usdc_transfer"):
                    print(f"\n   USDC Transfer: {exec_result['usdc_transfer']}")
                if exec_result.get("hype_trade"):
                    print(f"   HYPE Trade: {exec_result['hype_trade']}")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
