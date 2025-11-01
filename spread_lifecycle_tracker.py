#!/usr/bin/env python3
"""
SPREAD LIFECYCLE TRACKER
========================

CRITICAL QUESTION: When spread opens and closes, which side moves?

PHASES:
1. NORMAL: Spread < 8 bps (baseline state)
2. SPIKE: Spread > 8 bps (anomaly detected!)
3. RETURN: Spread back to < 8 bps (normalized)

ANALYSIS:
- OPENING: Normal → Spike (which side caused it?)
- CLOSING: Spike → Normal (which side returned first?)

TARGET: 5 complete anomaly cycles
"""

import asyncio
import json
import time
from datetime import datetime
from collections import deque
from statistics import mean

import websockets


class SpreadLifecycleTracker:
    def __init__(self):
        # Price tracking
        self.perp_bids = deque(maxlen=50)
        self.perp_asks = deque(maxlen=50)
        self.spot_bids = deque(maxlen=50)
        self.spot_asks = deque(maxlen=50)

        # Baseline (20-tick rolling average)
        self.perp_bid_baseline = None
        self.perp_ask_baseline = None
        self.spot_bid_baseline = None
        self.spot_ask_baseline = None

        # State tracking
        self.state = "NORMAL"  # NORMAL, SPIKE, COOLDOWN
        self.spike_start_time = None
        self.spike_start_prices = None
        self.baseline_at_spike = None

        # Anomaly data
        self.anomalies = []
        self.current_anomaly = None
        self.tick_count = 0
        self.last_print = time.time()

        print("=" * 90)
        print("🔬 SPREAD LIFECYCLE TRACKER - WHO MOVES WHEN?")
        print("=" * 90)
        print()
        print("🎯 OBJECTIVE:")
        print("   Track OPENING: Normal → 8+ bps (who caused spike?)")
        print("   Track CLOSING: 8+ bps → Normal (who returned first?)")
        print()
        print("📊 MONITORING:")
        print("   - PERP ask & SPOT bid (for perp->spot spread)")
        print("   - Baseline: 20-tick rolling average")
        print("   - Threshold: 8 bps (lowered for more data)")
        print()
        print("🎬 TARGET: 5 complete anomaly cycles (NO TIME LIMIT)")
        print("=" * 90)
        print()

    def update_baseline(self):
        """Update rolling baseline (last 20 ticks)"""
        if len(self.perp_asks) >= 20:
            self.perp_bid_baseline = mean(list(self.perp_bids)[-20:])
            self.perp_ask_baseline = mean(list(self.perp_asks)[-20:])
            self.spot_bid_baseline = mean(list(self.spot_bids)[-20:])
            self.spot_ask_baseline = mean(list(self.spot_asks)[-20:])

    def calculate_movements(self, perp_bid, perp_ask, spot_bid, spot_ask):
        """Calculate price movements from baseline (in bps)"""
        if not self.perp_ask_baseline:
            return None

        mid = (perp_bid + perp_ask + spot_bid + spot_ask) / 4

        return {
            'perp_bid_dev_bps': (perp_bid - self.perp_bid_baseline) / mid * 10000,
            'perp_ask_dev_bps': (perp_ask - self.perp_ask_baseline) / mid * 10000,
            'spot_bid_dev_bps': (spot_bid - self.spot_bid_baseline) / mid * 10000,
            'spot_ask_dev_bps': (spot_ask - self.spot_ask_baseline) / mid * 10000,
            'perp_ask_movement': abs(perp_ask - self.perp_ask_baseline) / mid * 10000,
            'spot_bid_movement': abs(spot_bid - self.spot_bid_baseline) / mid * 10000
        }

    def analyze_opening(self, movements):
        """Analyze who caused the spread to spike"""
        perp_mov = movements['perp_ask_movement']
        spot_mov = movements['spot_bid_movement']

        # For perp->spot spread to increase:
        # - PERP ask DROPS (negative deviation) = perp cheaper
        # - SPOT bid RISES (positive deviation) = spot more expensive

        if perp_mov > spot_mov * 1.5:
            return "PERP", perp_mov, spot_mov
        elif spot_mov > perp_mov * 1.5:
            return "SPOT", spot_mov, perp_mov
        else:
            return "BOTH", max(perp_mov, spot_mov), min(perp_mov, spot_mov)

    def analyze_closing(self, movements):
        """Analyze who returned to baseline first"""
        perp_mov = movements['perp_ask_movement']
        spot_mov = movements['spot_bid_movement']

        # Smaller movement = closer to baseline = returned first
        if perp_mov < spot_mov * 0.67:
            return "PERP", perp_mov, spot_mov
        elif spot_mov < perp_mov * 0.67:
            return "SPOT", spot_mov, perp_mov
        else:
            return "BOTH", min(perp_mov, spot_mov), max(perp_mov, spot_mov)

    def process_tick(self, perp_bid, perp_ask, spot_bid, spot_ask):
        """Process market data tick"""
        self.tick_count += 1
        now = time.time()

        # Store prices
        self.perp_bids.append(perp_bid)
        self.perp_asks.append(perp_ask)
        self.spot_bids.append(spot_bid)
        self.spot_asks.append(spot_ask)

        # Update baseline
        self.update_baseline()

        # Calculate spread
        mid = (perp_bid + perp_ask + spot_bid + spot_ask) / 4
        spread_bps = (spot_bid - perp_ask) / mid * 10000

        # Real-time display (every 2 seconds)
        if now - self.last_print > 2:
            baseline_str = ""
            if self.perp_ask_baseline:
                baseline_str = f"| Base: P={self.perp_ask_baseline:.3f} S={self.spot_bid_baseline:.3f}"

            state_emoji = {"NORMAL": "🟢", "SPIKE": "🔴", "COOLDOWN": "🟡"}

            print(f"[{self.tick_count:5d}] {state_emoji.get(self.state, '⚪')} "
                  f"Spread: {spread_bps:7.2f} bps | P_ask: {perp_ask:.3f} | S_bid: {spot_bid:.3f} {baseline_str}")
            self.last_print = now

        # State machine
        if self.state == "NORMAL":
            # Waiting for spike
            if spread_bps > 8:
                self.state = "SPIKE"
                self.spike_start_time = now
                self.spike_start_prices = {
                    'perp_bid': perp_bid,
                    'perp_ask': perp_ask,
                    'spot_bid': spot_bid,
                    'spot_ask': spot_ask
                }
                self.baseline_at_spike = {
                    'perp_bid': self.perp_bid_baseline,
                    'perp_ask': self.perp_ask_baseline,
                    'spot_bid': self.spot_bid_baseline,
                    'spot_ask': self.spot_ask_baseline
                }

                movements = self.calculate_movements(perp_bid, perp_ask, spot_bid, spot_ask)
                if movements:
                    source, primary_mov, secondary_mov = self.analyze_opening(movements)

                    self.current_anomaly = {
                        'anomaly_num': len(self.anomalies) + 1,
                        'spike_time': now,
                        'spike_spread_bps': spread_bps,
                        'spike_prices': self.spike_start_prices.copy(),
                        'baseline': self.baseline_at_spike.copy(),
                        'opening_source': source,
                        'opening_primary_movement': primary_mov,
                        'opening_secondary_movement': secondary_mov,
                        'opening_movements': movements.copy()
                    }

                    print()
                    print("🚨" * 45)
                    print(f"🔴 SPIKE #{len(self.anomalies) + 1} DETECTED!")
                    print(f"Time: {datetime.fromtimestamp(now).strftime('%H:%M:%S')}")
                    print(f"Spread: {spread_bps:.2f} bps")
                    print()
                    print("📈 OPENING ANALYSIS (Normal → Spike):")
                    print(f"   PERP ask: {self.perp_ask_baseline:.3f} → {perp_ask:.3f} (Δ {movements['perp_ask_dev_bps']:+.2f} bps)")
                    print(f"   SPOT bid: {self.spot_bid_baseline:.3f} → {spot_bid:.3f} (Δ {movements['spot_bid_dev_bps']:+.2f} bps)")
                    print()
                    print(f"   PERP movement: {primary_mov if source == 'PERP' else secondary_mov:.2f} bps")
                    print(f"   SPOT movement: {primary_mov if source == 'SPOT' else secondary_mov:.2f} bps")
                    print()
                    print(f"🎯 OPENING SOURCE: {source}")
                    if source == "PERP":
                        print("   → PERP moved MORE to cause spike!")
                        print("   → SPOT was STABLE during opening!")
                        print("   → ✅ ALO SPOT looks GOOD for opening!")
                    elif source == "SPOT":
                        print("   → SPOT moved MORE to cause spike!")
                        print("   → PERP was STABLE during opening!")
                        print("   → ⚠️  ALO SPOT might get rejected!")
                    else:
                        print("   → BOTH moved together!")
                        print("   → ⚠️  Synchronized movement, ALO risky!")
                    print()
                    print("   ⏳ Now watching for return to normal...")
                    print("🚨" * 45)
                    print()

        elif self.state == "SPIKE":
            # Waiting for return to normal
            if spread_bps < 8:
                self.state = "COOLDOWN"

                movements = self.calculate_movements(perp_bid, perp_ask, spot_bid, spot_ask)
                if movements and self.current_anomaly:
                    source, primary_mov, secondary_mov = self.analyze_closing(movements)

                    duration = now - self.spike_start_time

                    self.current_anomaly.update({
                        'return_time': now,
                        'return_spread_bps': spread_bps,
                        'duration_seconds': duration,
                        'return_prices': {
                            'perp_bid': perp_bid,
                            'perp_ask': perp_ask,
                            'spot_bid': spot_bid,
                            'spot_ask': spot_ask
                        },
                        'closing_source': source,
                        'closing_primary_movement': primary_mov,
                        'closing_secondary_movement': secondary_mov,
                        'closing_movements': movements.copy()
                    })

                    self.anomalies.append(self.current_anomaly)

                    print()
                    print("🟢" * 45)
                    print(f"🟢 SPIKE #{len(self.anomalies)} CLOSED!")
                    print(f"Duration: {duration:.1f} seconds")
                    print()
                    print("📉 CLOSING ANALYSIS (Spike → Normal):")
                    print(f"   PERP ask: {self.spike_start_prices['perp_ask']:.3f} → {perp_ask:.3f} (now {movements['perp_ask_movement']:.2f} bps from baseline)")
                    print(f"   SPOT bid: {self.spike_start_prices['spot_bid']:.3f} → {spot_bid:.3f} (now {movements['spot_bid_movement']:.2f} bps from baseline)")
                    print()
                    print(f"   PERP distance from baseline: {primary_mov if source == 'PERP' else secondary_mov:.2f} bps")
                    print(f"   SPOT distance from baseline: {primary_mov if source == 'SPOT' else secondary_mov:.2f} bps")
                    print()
                    print(f"🎯 CLOSING SOURCE: {source}")
                    if source == "PERP":
                        print("   → PERP returned to baseline FIRST!")
                        print("   → SPOT still far from baseline!")
                        print("   → ✅ PERP is MORE STABLE!")
                    elif source == "SPOT":
                        print("   → SPOT returned to baseline FIRST!")
                        print("   → PERP still far from baseline!")
                        print("   → ✅ SPOT is MORE STABLE!")
                    else:
                        print("   → BOTH returned together!")
                    print()
                    print(f"📊 SUMMARY FOR SPIKE #{len(self.anomalies)}:")
                    print(f"   Opening: {self.current_anomaly['opening_source']} caused spike")
                    print(f"   Closing: {source} returned first")
                    print()

                    # Strategic insight
                    if self.current_anomaly['opening_source'] == "PERP" and source == "SPOT":
                        print("   💡 PERP opened spike, SPOT closed → MIXED signals")
                    elif self.current_anomaly['opening_source'] == "PERP" and source == "PERP":
                        print("   💡 PERP both opened AND closed → PERP is volatile, SPOT stable!")
                        print("   💡 ✅✅✅ STRONG SIGNAL for ALO SPOT!")
                    elif self.current_anomaly['opening_source'] == "SPOT":
                        print("   💡 SPOT caused spike → ⚠️  SPOT is volatile!")

                    print("🟢" * 45)
                    print()

                    # Stop if we have 5 anomalies
                    if len(self.anomalies) >= 5:
                        print("✅ TARGET REACHED: 5 anomalies collected!")
                        return True

                    self.current_anomaly = None

                    # Brief cooldown before detecting next spike
                    print(f"⏳ Cooldown for 5 seconds before next detection...")
                    print()

        elif self.state == "COOLDOWN":
            # Wait 5 seconds in cooldown
            if now - self.current_anomaly.get('return_time', now) > 5 if self.current_anomaly else True:
                self.state = "NORMAL"
                if now - self.last_print > 2:
                    print(f"🟢 Back to NORMAL mode. Waiting for next spike...")
                    print()

        return False

    def print_summary(self):
        """Print final analysis"""
        if not self.anomalies:
            print()
            print("=" * 90)
            print("⚠️  NO ANOMALIES DETECTED")
            print("=" * 90)
            print(f"Total ticks: {self.tick_count}")
            print("No spreads >10 bps observed. Try running longer or during active trading hours.")
            print("=" * 90)
            return

        print()
        print("=" * 90)
        print("📊 FINAL ANALYSIS - LIFECYCLE VOLATILITY")
        print("=" * 90)
        print(f"Total ticks: {self.tick_count}")
        print(f"Total anomalies: {len(self.anomalies)}")
        print()

        # Classify opening sources
        opening_perp = sum(1 for a in self.anomalies if a['opening_source'] == 'PERP')
        opening_spot = sum(1 for a in self.anomalies if a['opening_source'] == 'SPOT')
        opening_both = sum(1 for a in self.anomalies if a['opening_source'] == 'BOTH')

        # Classify closing sources
        closing_perp = sum(1 for a in self.anomalies if a['closing_source'] == 'PERP')
        closing_spot = sum(1 for a in self.anomalies if a['closing_source'] == 'SPOT')
        closing_both = sum(1 for a in self.anomalies if a['closing_source'] == 'BOTH')

        total = len(self.anomalies)

        print("📈 OPENING (Normal → Spike):")
        print(f"   PERP-driven: {opening_perp}/{total} ({opening_perp/total*100:.1f}%)")
        print(f"   SPOT-driven: {opening_spot}/{total} ({opening_spot/total*100:.1f}%)")
        print(f"   BOTH-driven: {opening_both}/{total} ({opening_both/total*100:.1f}%)")
        print()

        print("📉 CLOSING (Spike → Normal):")
        print(f"   PERP returned first: {closing_perp}/{total} ({closing_perp/total*100:.1f}%)")
        print(f"   SPOT returned first: {closing_spot}/{total} ({closing_spot/total*100:.1f}%)")
        print(f"   BOTH returned together: {closing_both}/{total} ({closing_both/total*100:.1f}%)")
        print()

        # Combined analysis
        perp_both = sum(1 for a in self.anomalies
                       if a['opening_source'] == 'PERP' and a['closing_source'] == 'PERP')
        spot_stable = sum(1 for a in self.anomalies
                         if a['opening_source'] == 'PERP' and a['closing_source'] == 'SPOT')

        print("🔬 COMBINED ANALYSIS:")
        print(f"   PERP opened AND closed: {perp_both}/{total} ({perp_both/total*100:.1f}%)")
        print(f"      → PERP is volatile, SPOT is stable!")
        print(f"   PERP opened, SPOT closed: {spot_stable}/{total} ({spot_stable/total*100:.1f}%)")
        print(f"      → Mixed signals")
        print()

        # Strategic recommendation
        print("=" * 90)
        print("🎯 STRATEGIC RECOMMENDATION")
        print("=" * 90)

        if opening_perp / total >= 0.7:
            print("✅✅✅ STRONG SIGNAL: PERP causes most spikes!")
            print()
            print("   This means:")
            print("   - PERP price is VOLATILE (jumps around)")
            print("   - SPOT price is STABLE (stays steady)")
            print()
            print("   💰 RECOMMENDED STRATEGY:")
            print("      Opening:")
            print("         ✅ PERP → IOC (catch volatile perp)")
            print("         ✅ SPOT → ALO (stable, save 3 bps!)")
            print()
            print("      Opening cost: 8.5 bps (vs 11.5 bps current)")
            print("      Total cost: ~14 bps (vs 18.2 bps current)")
            print("      Profit at 20 bps: 6 bps (vs 1.8 bps current)")
            print("      🚀 +233% PROFIT BOOST!")
            print()
            print("   ✅ IMPLEMENT ALO SPOT IMMEDIATELY!")

        elif opening_spot / total >= 0.7:
            print("⚠️  CAUTION: SPOT causes most spikes!")
            print()
            print("   This is unusual. SPOT is more volatile than PERP.")
            print("   ❌ DO NOT use ALO for SPOT")
            print("   ✅ Keep current strategy: IOC both")

        else:
            print("⚠️  MIXED RESULTS")
            print()
            print(f"   PERP-driven: {opening_perp/total*100:.1f}%")
            print(f"   SPOT-driven: {opening_spot/total*100:.1f}%")
            print(f"   BOTH-driven: {opening_both/total*100:.1f}%")
            print()
            print("   Need more data or volatility is evenly distributed.")
            print("   🧪 Consider small-size test with ALO spot")

        print("=" * 90)

        # Export data
        with open('lifecycle_analysis.json', 'w') as f:
            json.dump({
                'summary': {
                    'total_ticks': self.tick_count,
                    'total_anomalies': total,
                    'opening_perp_pct': opening_perp/total*100,
                    'opening_spot_pct': opening_spot/total*100,
                    'closing_perp_pct': closing_perp/total*100,
                    'closing_spot_pct': closing_spot/total*100
                },
                'anomalies': self.anomalies
            }, f, indent=2)
        print()
        print("💾 Detailed data saved to: lifecycle_analysis.json")
        print()


async def run_tracker():
    """Run the lifecycle tracker"""
    tracker = SpreadLifecycleTracker()

    uri = "wss://api.hyperliquid.xyz/ws"

    print(f"🔌 Connecting to {uri}...")

    async with websockets.connect(uri) as ws:
        print(f"✅ Connected!")

        # Subscribe
        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "l2Book", "coin": "HYPE"}
        }))

        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "l2Book", "coin": "@107"}
        }))

        print("✅ Subscribed to HYPE perp and spot")
        print()
        print("🎬 Starting lifecycle tracking...")
        print("   Waiting for spreads >10 bps...")
        print()

        perp_book = None
        spot_book = None
        start_time = time.time()

        try:
            async for message in ws:
                data = json.loads(message)

                if data.get("channel") == "subscriptionResponse":
                    continue

                if data.get("channel") == "l2Book":
                    book_data = data.get("data", {})
                    coin = book_data.get("coin")
                    levels = book_data.get("levels")

                    if not levels or len(levels) < 2:
                        continue

                    if coin == "HYPE":
                        if levels[0] and levels[1]:
                            perp_book = {
                                'bid': float(levels[0][0]["px"]),
                                'ask': float(levels[1][0]["px"])
                            }
                    elif coin == "@107":
                        if levels[0] and levels[1]:
                            spot_book = {
                                'bid': float(levels[0][0]["px"]),
                                'ask': float(levels[1][0]["px"])
                            }

                    if perp_book and spot_book:
                        done = tracker.process_tick(
                            perp_book['bid'],
                            perp_book['ask'],
                            spot_book['bid'],
                            spot_book['ask']
                        )

                        if done:
                            break

                # No time limit - run until 5 anomalies found

        except KeyboardInterrupt:
            print()
            print("⚠️  Stopped by user")

        tracker.print_summary()


if __name__ == "__main__":
    print()
    try:
        asyncio.run(run_tracker())
    except KeyboardInterrupt:
        print()
        print("⚠️  Interrupted")

    print()
    print("✅ Lifecycle analysis complete!")
    print()
