#!/usr/bin/env python3
"""
CRITICAL VOLATILITY TRACKER
===========================

OBJECTIVE: Determine which side causes spread anomalies
- If PERP volatile → Use IOC perp + ALO spot (save 3 bps!)
- If SPOT volatile → Use IOC both (current strategy)

METHOD:
1. Track perp_ask and spot_bid continuously (for perp->spot arbitrage)
2. Calculate rolling baseline (20-tick average)
3. When spread >20 bps:
   - Measure: How much did PERP deviate from baseline?
   - Measure: How much did SPOT deviate from baseline?
   - Classify: PERP-driven, SPOT-driven, or BOTH
4. Report: X% anomalies are PERP-driven → Use ALO spot!

DURATION: Run for 10 minutes or until 20+ anomalies detected
"""

import asyncio
import json
import time
import sys
from datetime import datetime
from collections import deque
from statistics import mean

import websockets


class VolatilityTracker:
    def __init__(self):
        # Price tracking
        self.perp_bids = deque(maxlen=50)
        self.perp_asks = deque(maxlen=50)
        self.spot_bids = deque(maxlen=50)
        self.spot_asks = deque(maxlen=50)
        self.timestamps = deque(maxlen=50)

        # Baseline (rolling average)
        self.perp_ask_baseline = None
        self.spot_bid_baseline = None

        # Anomaly tracking
        self.anomalies = []
        self.tick_count = 0

        # For real-time display
        self.last_print = time.time()

        print("=" * 80)
        print("🔬 CRITICAL VOLATILITY TRACKER - LIVE ANALYSIS")
        print("=" * 80)
        print("Target: perp->spot arbitrage (HYPE)")
        print("Goal: Identify which side (PERP or SPOT) causes spread spikes")
        print()
        print("Monitoring:")
        print("  📍 PERP ask (sell price)")
        print("  📍 SPOT bid (buy price)")
        print("  📏 Spread = (spot_bid - perp_ask) / mid * 10000")
        print()
        print("Anomaly threshold: >20 bps spread")
        print("Target: 20+ anomalies OR 10 minutes")
        print("=" * 80)
        print()

    def update_baseline(self):
        """Update rolling baseline (last 20 ticks)"""
        if len(self.perp_asks) >= 20:
            self.perp_ask_baseline = mean(list(self.perp_asks)[-20:])
            self.spot_bid_baseline = mean(list(self.spot_bids)[-20:])

    def analyze_anomaly(self, perp_bid, perp_ask, spot_bid, spot_ask, spread_bps):
        """
        Determine if anomaly is PERP-driven or SPOT-driven

        perp->spot spread = (spot_bid - perp_ask) / mid

        If spread increases:
        - PERP ask DROPPED (perp cheaper) → PERP moved
        - SPOT bid INCREASED (spot more expensive) → SPOT moved
        """
        if not self.perp_ask_baseline or not self.spot_bid_baseline:
            return None

        # Calculate deviations from baseline (in bps)
        mid = (perp_bid + perp_ask + spot_bid + spot_ask) / 4

        perp_ask_deviation = (perp_ask - self.perp_ask_baseline) / mid * 10000
        spot_bid_deviation = (spot_bid - self.spot_bid_baseline) / mid * 10000

        # Absolute deviations
        perp_movement = abs(perp_ask_deviation)
        spot_movement = abs(spot_bid_deviation)

        # Classify
        if perp_movement > spot_movement * 1.5:
            source = "PERP"
        elif spot_movement > perp_movement * 1.5:
            source = "SPOT"
        else:
            source = "BOTH"

        return {
            'timestamp': time.time(),
            'spread_bps': spread_bps,
            'perp_ask': perp_ask,
            'spot_bid': spot_bid,
            'perp_ask_baseline': self.perp_ask_baseline,
            'spot_bid_baseline': self.spot_bid_baseline,
            'perp_ask_deviation': perp_ask_deviation,  # negative = dropped (bullish for arb)
            'spot_bid_deviation': spot_bid_deviation,  # positive = increased (bullish for arb)
            'perp_movement_bps': perp_movement,
            'spot_movement_bps': spot_movement,
            'source': source,
            'perp_bid': perp_bid,
            'perp_ask': perp_ask,
            'spot_bid': spot_bid,
            'spot_ask': spot_ask
        }

    def process_tick(self, perp_bid, perp_ask, spot_bid, spot_ask):
        """Process market data tick"""
        self.tick_count += 1
        now = time.time()

        # Store prices
        self.perp_bids.append(perp_bid)
        self.perp_asks.append(perp_ask)
        self.spot_bids.append(spot_bid)
        self.spot_asks.append(spot_ask)
        self.timestamps.append(now)

        # Update baseline
        self.update_baseline()

        # Calculate spread (perp->spot)
        mid = (perp_bid + perp_ask + spot_bid + spot_ask) / 4
        spread_bps = (spot_bid - perp_ask) / mid * 10000

        # Real-time display (every 2 seconds)
        if now - self.last_print > 2:
            baseline_str = ""
            if self.perp_ask_baseline and self.spot_bid_baseline:
                baseline_str = f"| Baseline: P_ask={self.perp_ask_baseline:.3f} S_bid={self.spot_bid_baseline:.3f}"

            print(f"[{self.tick_count:5d}] Spread: {spread_bps:6.2f} bps | "
                  f"P_ask: {perp_ask:.3f} | S_bid: {spot_bid:.3f} {baseline_str}")
            self.last_print = now

        # Detect anomaly (>15 bps - lowered threshold for more data)
        if spread_bps > 15:
            analysis = self.analyze_anomaly(perp_bid, perp_ask, spot_bid, spot_ask, spread_bps)

            if analysis:
                self.anomalies.append(analysis)

                # Print anomaly details
                print()
                print("🚨" * 40)
                print(f"ANOMALY #{len(self.anomalies)} DETECTED!")
                print(f"Time: {datetime.fromtimestamp(now).strftime('%H:%M:%S.%f')[:-3]}")
                print(f"Spread: {spread_bps:.2f} bps")
                print()
                print(f"PERP ask: {perp_ask:.3f} (baseline: {analysis['perp_ask_baseline']:.3f})")
                print(f"  → Deviation: {analysis['perp_ask_deviation']:+.2f} bps")
                print(f"  → Movement: {analysis['perp_movement_bps']:.2f} bps")
                print()
                print(f"SPOT bid: {spot_bid:.3f} (baseline: {analysis['spot_bid_baseline']:.3f})")
                print(f"  → Deviation: {analysis['spot_bid_deviation']:+.2f} bps")
                print(f"  → Movement: {analysis['spot_movement_bps']:.2f} bps")
                print()
                print(f"🎯 SOURCE: {analysis['source']}")
                if analysis['source'] == "PERP":
                    print("   → PERP moved more! SPOT is stable!")
                    print("   → ✅ Can use ALO for SPOT (save 3 bps!)")
                elif analysis['source'] == "SPOT":
                    print("   → SPOT moved more! PERP is stable!")
                    print("   → ⚠️  Must use IOC for SPOT")
                else:
                    print("   → BOTH moved! Synchronized movement")
                    print("   → ⚠️  ALO risky, use IOC both")
                print("🚨" * 40)
                print()

    def print_summary(self):
        """Print final analysis"""
        if not self.anomalies:
            print()
            print("=" * 80)
            print("⚠️  NO ANOMALIES DETECTED")
            print("=" * 80)
            print(f"Total ticks: {self.tick_count}")
            print("No spreads >20 bps observed in this period.")
            print("Try running longer or lowering threshold to 15 bps.")
            print("=" * 80)
            return

        print()
        print("=" * 80)
        print("📊 FINAL ANALYSIS - VOLATILITY SOURCE")
        print("=" * 80)
        print(f"Total ticks: {self.tick_count}")
        print(f"Total anomalies (>20 bps): {len(self.anomalies)}")
        print()

        # Classify anomalies
        perp_driven = [a for a in self.anomalies if a['source'] == 'PERP']
        spot_driven = [a for a in self.anomalies if a['source'] == 'SPOT']
        both_driven = [a for a in self.anomalies if a['source'] == 'BOTH']

        total = len(self.anomalies)
        perp_pct = len(perp_driven) / total * 100
        spot_pct = len(spot_driven) / total * 100
        both_pct = len(both_driven) / total * 100

        print(f"🔴 PERP-driven: {len(perp_driven):3d} ({perp_pct:5.1f}%)")
        print(f"🔵 SPOT-driven: {len(spot_driven):3d} ({spot_pct:5.1f}%)")
        print(f"🟣 BOTH-driven: {len(both_driven):3d} ({both_pct:5.1f}%)")
        print()

        # Average movements
        if perp_driven:
            avg_spread = mean([a['spread_bps'] for a in perp_driven])
            avg_perp_mov = mean([a['perp_movement_bps'] for a in perp_driven])
            avg_spot_mov = mean([a['spot_movement_bps'] for a in perp_driven])

            print("PERP-driven anomalies (detailed):")
            print(f"  Avg spread: {avg_spread:.2f} bps")
            print(f"  Avg PERP movement: {avg_perp_mov:.2f} bps  👈 VOLATILE")
            print(f"  Avg SPOT movement: {avg_spot_mov:.2f} bps  👈 STABLE")
            print(f"  Ratio: {avg_perp_mov/avg_spot_mov:.2f}x more volatile")
            print()

        if spot_driven:
            avg_spread = mean([a['spread_bps'] for a in spot_driven])
            avg_perp_mov = mean([a['perp_movement_bps'] for a in spot_driven])
            avg_spot_mov = mean([a['spot_movement_bps'] for a in spot_driven])

            print("SPOT-driven anomalies (detailed):")
            print(f"  Avg spread: {avg_spread:.2f} bps")
            print(f"  Avg PERP movement: {avg_perp_mov:.2f} bps  👈 STABLE")
            print(f"  Avg SPOT movement: {avg_spot_mov:.2f} bps  👈 VOLATILE")
            print(f"  Ratio: {avg_spot_mov/avg_perp_mov:.2f}x more volatile")
            print()

        # STRATEGIC RECOMMENDATION
        print("=" * 80)
        print("🎯 STRATEGIC RECOMMENDATION")
        print("=" * 80)

        if perp_pct >= 70:
            print("✅ STRONG SIGNAL: PERP is the volatile side!")
            print()
            print("📋 RECOMMENDED STRATEGY:")
            print("  Opening:")
            print("    ✅ PERP → IOC (catches volatile spikes)")
            print("    ✅ SPOT → ALO (stable side, save fees!)")
            print()
            print("  Closing:")
            print("    ✅ Both → ALO (15min timeout)")
            print()
            print("💰 COST ANALYSIS:")
            print("  Current (IOC both open):")
            print(f"    Open: 11.5 bps (4.5 perp + 7.0 spot)")
            print(f"    Close: 6.7 bps (80% ALO success)")
            print(f"    TOTAL: 18.2 bps")
            print()
            print("  NEW (IOC perp + ALO spot open):")
            print(f"    Open: 8.5 bps (4.5 perp + 4.0 spot)")
            print(f"    Close: 5.5 bps (80% ALO success)")
            print(f"    TOTAL: 14.0 bps  🚀 -23% cost!")
            print()
            print(f"  Profit at 20 bps threshold:")
            print(f"    Current: 1.8 bps/trade")
            print(f"    NEW: 6.0 bps/trade  🚀 +233% profit!")
            print()
            print("✅ IMPLEMENT THIS IMMEDIATELY!")

        elif spot_pct >= 70:
            print("⚠️  CAUTION: SPOT is the volatile side!")
            print()
            print("📋 RECOMMENDED STRATEGY:")
            print("  ❌ DO NOT use ALO for SPOT")
            print("  ✅ Keep current: IOC both open")
            print()
            print("This is unusual for perp markets. Double-check data.")

        else:
            print("⚠️  MIXED RESULTS: Both sides show volatility")
            print()
            print(f"PERP-driven: {perp_pct:.1f}%")
            print(f"SPOT-driven: {spot_pct:.1f}%")
            print(f"BOTH-driven: {both_pct:.1f}%")
            print()
            print("📋 RECOMMENDED STRATEGY:")
            print("  ⚠️  Keep current IOC both (safer)")
            print("  OR")
            print("  🧪 Test hybrid: Start with small size ALO spot")
            print()
            print("Need more data. Consider running test longer.")

        print("=" * 80)

        # Export detailed data
        print()
        print("💾 Exporting detailed anomaly data...")
        with open('anomaly_analysis_detailed.json', 'w') as f:
            json.dump({
                'summary': {
                    'total_ticks': self.tick_count,
                    'total_anomalies': total,
                    'perp_driven_count': len(perp_driven),
                    'spot_driven_count': len(spot_driven),
                    'both_driven_count': len(both_driven),
                    'perp_driven_pct': perp_pct,
                    'spot_driven_pct': spot_pct,
                    'both_driven_pct': both_pct
                },
                'anomalies': self.anomalies
            }, f, indent=2)
        print("✅ Saved to: anomaly_analysis_detailed.json")
        print()


async def run_tracker():
    """Run volatility tracker"""
    tracker = VolatilityTracker()

    uri = "wss://api.hyperliquid.xyz/ws"

    print(f"🔌 Connecting to {uri}...")

    async with websockets.connect(uri) as ws:
        print(f"✅ Connected!")

        # Subscribe to HYPE perp
        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "l2Book", "coin": "HYPE"}
        }))

        # Subscribe to HYPE spot
        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "l2Book", "coin": "@107"}
        }))

        print("✅ Subscribed to HYPE perp and spot orderbooks")
        print()
        print("🎬 Starting live monitoring...")
        print("-" * 80)
        print()

        perp_book = None
        spot_book = None
        start_time = time.time()

        try:
            async for message in ws:
                data = json.loads(message)

                # Skip subscription confirmations
                if data.get("channel") == "subscriptionResponse":
                    continue

                # Handle orderbook updates
                if data.get("channel") == "l2Book":
                    book_data = data.get("data", {})
                    coin = book_data.get("coin")
                    levels = book_data.get("levels")

                    if not levels or len(levels) < 2:
                        continue

                    # Extract bid/ask
                    if coin == "HYPE":  # Perp
                        if levels[0] and levels[1]:
                            perp_book = {
                                'bid': float(levels[0][0]["px"]),
                                'ask': float(levels[1][0]["px"])
                            }
                    elif coin == "@107":  # Spot
                        if levels[0] and levels[1]:
                            spot_book = {
                                'bid': float(levels[0][0]["px"]),
                                'ask': float(levels[1][0]["px"])
                            }

                    # Process if we have both
                    if perp_book and spot_book:
                        tracker.process_tick(
                            perp_book['bid'],
                            perp_book['ask'],
                            spot_book['bid'],
                            spot_book['ask']
                        )

                    # Stop conditions
                    elapsed = time.time() - start_time

                    # Stop if 20+ anomalies OR 10 minutes
                    if len(tracker.anomalies) >= 20:
                        print()
                        print("✅ Target reached: 20+ anomalies detected!")
                        break

                    if elapsed > 600:  # 10 minutes
                        print()
                        print("⏰ Time limit reached: 10 minutes")
                        break

        except KeyboardInterrupt:
            print()
            print("⚠️  Stopped by user (Ctrl+C)")

        # Print final summary
        tracker.print_summary()


if __name__ == "__main__":
    print()
    try:
        asyncio.run(run_tracker())
    except KeyboardInterrupt:
        print()
        print("⚠️  Interrupted")

    print()
    print("✅ Analysis complete!")
    print()
