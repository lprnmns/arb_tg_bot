#!/usr/bin/env python3
"""
Anomaly Source Analyzer
========================

Analyzes edge spikes to determine if they come from:
1. PERP side movement (perp price spikes)
2. SPOT side movement (spot price spikes)
3. BOTH sides moving

This determines optimal opening strategy:
- If PERP-only → Use IOC perp + ALO spot (save 3 bps!)
- If SPOT-only → Use ALO perp + IOC spot
- If BOTH → Use IOC both (current strategy)
"""

import asyncio
import json
import time
from datetime import datetime
from collections import deque
from statistics import mean, stdev

import websockets


class AnomalyAnalyzer:
    def __init__(self, window_seconds=300):
        self.window_seconds = window_seconds
        self.perp_bids = deque(maxlen=1000)
        self.perp_asks = deque(maxlen=1000)
        self.spot_bids = deque(maxlen=1000)
        self.spot_asks = deque(maxlen=1000)
        self.timestamps = deque(maxlen=1000)

        # Track anomalies (>15 bps edges)
        self.anomalies = []

        # Baseline tracking
        self.perp_bid_baseline = None
        self.perp_ask_baseline = None
        self.spot_bid_baseline = None
        self.spot_ask_baseline = None

        print("🔬 Anomaly Source Analyzer Started")
        print("=" * 60)
        print("Tracking PERP vs SPOT movements during edge spikes...")
        print()

    def update_baseline(self):
        """Update baseline prices (rolling average)"""
        if len(self.perp_bids) >= 20:
            self.perp_bid_baseline = mean(list(self.perp_bids)[-20:])
            self.perp_ask_baseline = mean(list(self.perp_asks)[-20:])
            self.spot_bid_baseline = mean(list(self.spot_bids)[-20:])
            self.spot_ask_baseline = mean(list(self.spot_asks)[-20:])

    def analyze_movement(self, perp_bid, perp_ask, spot_bid, spot_ask, edge):
        """Analyze if movement is from perp or spot"""
        if not self.perp_bid_baseline:
            return None

        # Calculate % deviation from baseline
        perp_bid_dev = abs(perp_bid - self.perp_bid_baseline) / self.perp_bid_baseline * 10000  # bps
        perp_ask_dev = abs(perp_ask - self.perp_ask_baseline) / self.perp_ask_baseline * 10000
        spot_bid_dev = abs(spot_bid - self.spot_bid_baseline) / self.spot_bid_baseline * 10000
        spot_ask_dev = abs(spot_ask - self.spot_ask_baseline) / self.spot_ask_baseline * 10000

        perp_movement = max(perp_bid_dev, perp_ask_dev)
        spot_movement = max(spot_bid_dev, spot_ask_dev)

        return {
            'perp_movement_bps': perp_movement,
            'spot_movement_bps': spot_movement,
            'perp_bid_dev': perp_bid_dev,
            'perp_ask_dev': perp_ask_dev,
            'spot_bid_dev': spot_bid_dev,
            'spot_ask_dev': spot_ask_dev,
            'edge': edge
        }

    def process_tick(self, perp_bid, perp_ask, spot_bid, spot_ask):
        """Process a market data tick"""
        now = time.time()

        # Store data
        self.perp_bids.append(perp_bid)
        self.perp_asks.append(perp_ask)
        self.spot_bids.append(spot_bid)
        self.spot_asks.append(spot_ask)
        self.timestamps.append(now)

        # Update baseline
        self.update_baseline()

        # Calculate edge (perp->spot)
        mid = (perp_bid + perp_ask + spot_bid + spot_ask) / 4
        edge_ps = (spot_bid - perp_ask) / mid * 10000  # bps
        edge_sp = (perp_bid - spot_ask) / mid * 10000  # bps

        # Check for anomaly (>15 bps)
        if abs(edge_ps) > 15 or abs(edge_sp) > 15:
            movement = self.analyze_movement(perp_bid, perp_ask, spot_bid, spot_ask, max(abs(edge_ps), abs(edge_sp)))
            if movement:
                movement['timestamp'] = now
                movement['edge_ps'] = edge_ps
                movement['edge_sp'] = edge_sp
                self.anomalies.append(movement)

                # Real-time logging
                source = "PERP" if movement['perp_movement_bps'] > movement['spot_movement_bps'] * 1.5 else \
                         "SPOT" if movement['spot_movement_bps'] > movement['perp_movement_bps'] * 1.5 else \
                         "BOTH"

                print(f"🚨 ANOMALY DETECTED!")
                print(f"   Time: {datetime.fromtimestamp(now).strftime('%H:%M:%S')}")
                print(f"   Edge: {edge_ps:.1f} / {edge_sp:.1f} bps")
                print(f"   Perp Movement: {movement['perp_movement_bps']:.1f} bps")
                print(f"   Spot Movement: {movement['spot_movement_bps']:.1f} bps")
                print(f"   🎯 SOURCE: {source}")
                print()

    def print_summary(self):
        """Print analysis summary"""
        if not self.anomalies:
            print("⚠️  No anomalies detected yet. Keep monitoring...")
            return

        print("\n" + "=" * 60)
        print("📊 ANOMALY SOURCE ANALYSIS")
        print("=" * 60)
        print(f"Total anomalies: {len(self.anomalies)}")
        print()

        # Classify anomalies
        perp_only = []
        spot_only = []
        both = []

        for a in self.anomalies:
            ratio = a['perp_movement_bps'] / max(a['spot_movement_bps'], 0.1)
            if ratio > 1.5:  # Perp moved 50%+ more
                perp_only.append(a)
            elif ratio < 0.67:  # Spot moved 50%+ more
                spot_only.append(a)
            else:
                both.append(a)

        total = len(self.anomalies)
        print(f"🔴 PERP-driven anomalies: {len(perp_only)} ({len(perp_only)/total*100:.1f}%)")
        print(f"🔵 SPOT-driven anomalies: {len(spot_only)} ({len(spot_only)/total*100:.1f}%)")
        print(f"🟣 BOTH-driven anomalies: {len(both)} ({len(both)/total*100:.1f}%)")
        print()

        # Average movements
        if perp_only:
            avg_perp_mov = mean([a['perp_movement_bps'] for a in perp_only])
            avg_spot_mov = mean([a['spot_movement_bps'] for a in perp_only])
            print(f"PERP-only anomalies:")
            print(f"   Avg perp movement: {avg_perp_mov:.1f} bps")
            print(f"   Avg spot movement: {avg_spot_mov:.1f} bps (stable!)")
            print()

        # Strategic recommendation
        print("🎯 STRATEGIC RECOMMENDATION:")
        perp_pct = len(perp_only) / total * 100
        spot_pct = len(spot_only) / total * 100

        if perp_pct > 60:
            print("   ✅ Use IOC for PERP (volatile)")
            print("   ✅ Use ALO for SPOT (stable)")
            print(f"   💰 Estimated opening cost: 8.5 bps (vs 11.5 bps current)")
            print(f"   💰 Total cost: 14.0 bps (vs 18.2 bps current)")
            print(f"   🚀 Profit increase: +3.3x at 20 bps threshold!")
        elif spot_pct > 60:
            print("   ✅ Use ALO for PERP (stable)")
            print("   ✅ Use IOC for SPOT (volatile)")
            print(f"   💰 Estimated opening cost: 8.5 bps")
        else:
            print("   ⚠️  BOTH sides volatile")
            print("   ✅ Keep current strategy: IOC for both")

        print("=" * 60)
        print()


async def run_analyzer():
    """Run the analyzer by connecting to Hyperliquid WebSocket"""
    analyzer = AnomalyAnalyzer()

    uri = "wss://api.hyperliquid.xyz/ws"

    async with websockets.connect(uri) as ws:
        print(f"📡 Connected to {uri}")

        # Subscribe to HYPE perp
        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "l2Book", "coin": "HYPE"}
        }))

        # Subscribe to HYPE spot (@107)
        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "l2Book", "coin": "@107"}
        }))

        print("✅ Subscribed to HYPE perp and spot")
        print()
        print("⏳ Monitoring market data (will analyze for 5 minutes)...")
        print()

        perp_book = None
        spot_book = None

        start_time = time.time()
        last_summary = start_time

        try:
            async for message in ws:
                data = json.loads(message)

                # Handle subscription response
                if data.get("channel") == "subscriptionResponse":
                    continue

                # Handle orderbook updates
                if data.get("channel") == "l2Book":
                    book_data = data.get("data", {})
                    coin = book_data.get("coin")
                    levels = book_data.get("levels")

                    if not levels or len(levels) < 2:
                        continue

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

                    # Process if we have both books
                    if perp_book and spot_book:
                        analyzer.process_tick(
                            perp_book['bid'],
                            perp_book['ask'],
                            spot_book['bid'],
                            spot_book['ask']
                        )

                # Print summary every 60 seconds
                now = time.time()
                if now - last_summary > 60:
                    analyzer.print_summary()
                    last_summary = now

                # Stop after 5 minutes
                if now - start_time > 300:
                    print("\n⏱️  5 minutes elapsed. Generating final report...")
                    break

        except KeyboardInterrupt:
            print("\n\n⚠️  Interrupted by user")

        # Final summary
        analyzer.print_summary()


if __name__ == "__main__":
    asyncio.run(run_analyzer())
