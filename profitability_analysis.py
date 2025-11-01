#!/usr/bin/env python3
"""
Profitability Analysis for IOC Open + ALO Close Strategy
==========================================================

Analyzes profitability at different BPS thresholds with:
- IOC open (11.5 bps taker fees)
- ALO close (5.5 bps maker fees) OR IOC fallback (11.5 bps)

Two scenarios:
1. Current balance: $45 spot + $16 perp = $61
2. Large balance: $200 spot + $100 perp = $300
"""

from typing import Dict, List


# ============================================================================
# FEE STRUCTURE
# ============================================================================

FEES = {
    "perp_maker": 1.5,   # bps
    "perp_taker": 4.5,   # bps
    "spot_maker": 4.0,   # bps
    "spot_taker": 7.0,   # bps
}

# Total fees (perp + spot, one side)
MAKER_TOTAL = FEES["perp_maker"] + FEES["spot_maker"]  # 5.5 bps
TAKER_TOTAL = FEES["perp_taker"] + FEES["spot_taker"]  # 11.5 bps


# ============================================================================
# COST CALCULATIONS
# ============================================================================

def calculate_costs(alo_success_rate: float = 0.8) -> Dict:
    """
    Calculate round-trip costs for different scenarios.

    Args:
        alo_success_rate: Probability that ALO closes successfully (default 80%)

    Returns:
        {
            "open_cost": bps,
            "close_alo": bps,
            "close_ioc": bps,
            "close_weighted": bps (weighted average based on success rate),
            "total_alo": bps (if ALO succeeds),
            "total_ioc": bps (if ALO fails, uses IOC),
            "total_weighted": bps (expected cost)
        }
    """
    # Open: Always IOC (taker)
    open_cost = TAKER_TOTAL

    # Close: ALO (maker) or IOC (taker)
    close_alo = MAKER_TOTAL
    close_ioc = TAKER_TOTAL

    # Weighted average for close (based on ALO success rate)
    close_weighted = alo_success_rate * close_alo + (1 - alo_success_rate) * close_ioc

    # Total round-trip costs
    total_alo = open_cost + close_alo  # Best case: ALO succeeds
    total_ioc = open_cost + close_ioc  # Worst case: ALO timeout, IOC fallback
    total_weighted = open_cost + close_weighted  # Expected case

    return {
        "open_cost": open_cost,
        "close_alo": close_alo,
        "close_ioc": close_ioc,
        "close_weighted": close_weighted,
        "total_alo": total_alo,        # 17 bps
        "total_ioc": total_ioc,        # 23 bps
        "total_weighted": total_weighted
    }


# ============================================================================
# PROFITABILITY ANALYSIS
# ============================================================================

def analyze_threshold(
    threshold_bps: float,
    alloc_per_trade: float,
    total_capital: float,
    max_positions: int,
    alo_success_rate: float = 0.8,
    trades_per_day: float = 3.0  # Assumption
) -> Dict:
    """
    Analyze profitability at a given threshold.

    Args:
        threshold_bps: Edge threshold (net of maker fees)
        alloc_per_trade: Capital allocation per trade
        total_capital: Total available capital
        max_positions: Maximum simultaneous positions
        alo_success_rate: Probability ALO succeeds (0.0-1.0)
        trades_per_day: Expected trades per day at this threshold

    Returns:
        Detailed profitability metrics
    """
    costs = calculate_costs(alo_success_rate)

    # Net edge per trade (threshold is already net of maker fees during entry)
    # But we still need to account for close fees
    raw_threshold = threshold_bps + MAKER_TOTAL  # Convert net -> gross

    # Net PNL per trade (in bps)
    net_pnl_alo_bps = raw_threshold - costs["total_alo"]
    net_pnl_ioc_bps = raw_threshold - costs["total_ioc"]
    net_pnl_weighted_bps = raw_threshold - costs["total_weighted"]

    # Net PNL in dollars
    net_pnl_alo_usd = (net_pnl_alo_bps / 10000) * alloc_per_trade
    net_pnl_ioc_usd = (net_pnl_ioc_bps / 10000) * alloc_per_trade
    net_pnl_weighted_usd = (net_pnl_weighted_bps / 10000) * alloc_per_trade

    # Daily/monthly projections
    daily_pnl = net_pnl_weighted_usd * trades_per_day
    monthly_pnl = daily_pnl * 30

    # ROI
    daily_roi = (daily_pnl / total_capital) * 100
    monthly_roi = (monthly_pnl / total_capital) * 100

    return {
        "threshold_bps": threshold_bps,
        "raw_threshold_bps": raw_threshold,
        "alloc_per_trade": alloc_per_trade,
        "total_capital": total_capital,
        "max_positions": max_positions,
        "alo_success_rate": alo_success_rate,
        "trades_per_day": trades_per_day,

        # Costs
        "costs": costs,

        # Net PNL per trade
        "net_pnl_alo_bps": net_pnl_alo_bps,
        "net_pnl_ioc_bps": net_pnl_ioc_bps,
        "net_pnl_weighted_bps": net_pnl_weighted_bps,
        "net_pnl_alo_usd": net_pnl_alo_usd,
        "net_pnl_ioc_usd": net_pnl_ioc_usd,
        "net_pnl_weighted_usd": net_pnl_weighted_usd,

        # Projections
        "daily_pnl": daily_pnl,
        "monthly_pnl": monthly_pnl,
        "daily_roi": daily_roi,
        "monthly_roi": monthly_roi,

        # Profitability flags
        "profitable_alo": net_pnl_alo_bps > 0,
        "profitable_ioc": net_pnl_ioc_bps > 0,
        "profitable_weighted": net_pnl_weighted_bps > 0,
    }


# ============================================================================
# SCENARIO ANALYSIS
# ============================================================================

def run_scenario_analysis():
    """Run analysis for both scenarios."""

    print("="*80)
    print("PROFITABILITY ANALYSIS: IOC OPEN + ALO CLOSE STRATEGY")
    print("="*80)
    print()
    print("Strategy:")
    print("  • OPEN:  100% IOC (taker fees: 11.5 bps)")
    print("  • CLOSE: ALO first (maker fees: 5.5 bps)")
    print("           → 5 min timeout")
    print("           → IOC fallback if timeout (taker fees: 11.5 bps)")
    print()

    # Scenarios
    scenarios = [
        {
            "name": "Current Balance",
            "spot": 45,
            "perp": 16,
            "total": 61,
            "alloc_per_trade": 19,  # $19 per trade
            "max_positions": 2,
            "leverage": 3
        },
        {
            "name": "Large Balance",
            "spot": 200,
            "perp": 100,
            "total": 300,
            "alloc_per_trade": 90,  # $90 per trade
            "max_positions": 2,
            "leverage": 3
        }
    ]

    # Thresholds to analyze
    thresholds = [20, 25, 30, 35, 40, 45, 50]

    # ALO success rates to test
    alo_success_rates = [0.5, 0.7, 0.8, 0.9]  # 50%, 70%, 80%, 90%

    # Expected trades per day at each threshold (assumptions based on HYPE volatility)
    trades_per_day_map = {
        20: 5.0,   # Very frequent
        25: 3.0,   # Frequent
        30: 2.0,   # Moderate
        35: 1.5,   # Less frequent
        40: 1.0,   # Rare
        45: 0.7,   # Very rare
        50: 0.5    # Extremely rare
    }

    for scenario in scenarios:
        print("="*80)
        print(f"📊 SCENARIO: {scenario['name']}")
        print("="*80)
        print(f"  Spot balance: ${scenario['spot']}")
        print(f"  Perp balance: ${scenario['perp']}")
        print(f"  Total capital: ${scenario['total']}")
        print(f"  Allocation per trade: ${scenario['alloc_per_trade']}")
        print(f"  Max positions: {scenario['max_positions']}")
        print(f"  Leverage: {scenario['leverage']}x")
        print()

        # Test different ALO success rates
        for alo_rate in alo_success_rates:
            print("-"*80)
            print(f"ALO Success Rate: {alo_rate*100:.0f}%")
            print("-"*80)
            print()

            # Table header
            print(f"{'Threshold':>10} | {'Net PNL':>10} | {'Daily PNL':>10} | {'Monthly PNL':>10} | {'Monthly ROI':>12} | {'Trades/Day':>11} | {'Status':>10}")
            print(f"{'(bps)':>10} | {'($/trade)':>10} | {'($)':>10} | {'($)':>10} | {'(%)':>12} | {'':>11} | {'':>10}")
            print("-"*80)

            for threshold in thresholds:
                analysis = analyze_threshold(
                    threshold_bps=threshold,
                    alloc_per_trade=scenario["alloc_per_trade"],
                    total_capital=scenario["total"],
                    max_positions=scenario["max_positions"],
                    alo_success_rate=alo_rate,
                    trades_per_day=trades_per_day_map[threshold]
                )

                # Format values
                net_pnl = analysis["net_pnl_weighted_usd"]
                daily_pnl = analysis["daily_pnl"]
                monthly_pnl = analysis["monthly_pnl"]
                monthly_roi = analysis["monthly_roi"]
                trades_day = analysis["trades_per_day"]

                status = "✅ Profit" if analysis["profitable_weighted"] else "❌ Loss"

                print(f"{threshold:>10} | ${net_pnl:>9.3f} | ${daily_pnl:>9.2f} | ${monthly_pnl:>9.2f} | {monthly_roi:>11.2f}% | {trades_day:>11.1f} | {status:>10}")

            print()

        print()

    # Cost breakdown summary
    print("="*80)
    print("📊 COST BREAKDOWN")
    print("="*80)
    print()

    for alo_rate in alo_success_rates:
        costs = calculate_costs(alo_rate)
        print(f"ALO Success Rate: {alo_rate*100:.0f}%")
        print(f"  Open (IOC):           {costs['open_cost']:.1f} bps")
        print(f"  Close (ALO):          {costs['close_alo']:.1f} bps")
        print(f"  Close (IOC fallback): {costs['close_ioc']:.1f} bps")
        print(f"  Close (weighted):     {costs['close_weighted']:.1f} bps")
        print(f"  Total (best case):    {costs['total_alo']:.1f} bps")
        print(f"  Total (worst case):   {costs['total_ioc']:.1f} bps")
        print(f"  Total (expected):     {costs['total_weighted']:.1f} bps")
        print()

    print("="*80)
    print("✅ Analysis complete!")
    print("="*80)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    run_scenario_analysis()
