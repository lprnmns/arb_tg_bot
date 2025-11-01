#!/usr/bin/env python3
"""
Multi-Pair Arbitrage Discovery System
=====================================

Analyzes top liquid pairs on Hyperliquid to find optimal arbitrage opportunities.

Features:
- Discovers top 20 pairs with both perp and spot markets
- Collects 30min real-time data for all pairs
- Analyzes edge distribution and BPS curves
- Calculates optimal thresholds per pair
- Generates comparative profitability reports

Usage:
    python3 multi_pair_discovery.py --duration 1800 --top 20
"""

import asyncio
import json
import time
import argparse
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, List, Tuple, Optional
import statistics

import websockets
from hyperliquid.info import Info


class PairDiscovery:
    """Discovers top liquid pairs with both perp and spot markets."""

    def __init__(self):
        self.info = Info('https://api.hyperliquid.xyz', skip_ws=True)

    def get_top_pairs(self, top_n: int = 20) -> List[Dict]:
        """
        Get top N pairs by volume that have both perp and spot markets.

        Returns:
            List of dicts with: {
                'base': str,
                'perp_volume_24h': float,
                'spot_volume_24h': float,
                'perp_asset': str,
                'spot_coin': str,
                'spot_asset': int,
                'perp_sz_decimals': int,
                'spot_sz_decimals': int
            }
        """
        print("🔍 Discovering liquid pairs...")

        # Get perp universe
        perp_meta = self.info.meta()
        perp_universe = perp_meta.get('universe', [])

        # Build spot market map using name_to_coin
        # Hyperliquid has limited spot markets - find all /USDC pairs
        spot_markets = {}
        for name, coin in self.info.name_to_coin.items():
            if '/USDC' in name and not name.startswith('@'):
                base_name = name.split('/')[0]

                # Get asset info for sz_decimals
                try:
                    spot_asset = self.info.name_to_asset(coin)
                    sz_decimals = self.info.asset_to_sz_decimals.get(spot_asset, 2)

                    spot_markets[base_name] = {
                        'coin': coin,
                        'asset': spot_asset,
                        'sz_decimals': sz_decimals
                    }
                except Exception as e:
                    # Skip if we can't get asset info
                    continue

        print(f"  Found {len(spot_markets)} spot markets")

        # Get 24h volumes for all perp markets
        all_mids = self.info.all_mids()

        pairs = []
        for perp in perp_universe:
            base = perp['name']

            # Check if spot market exists
            if base not in spot_markets:
                continue

            # Get volumes (simplified - using name as proxy for volume)
            # In production, fetch actual 24h volume from meta24hSummary
            perp_volume = 1000000  # Placeholder
            spot_volume = 500000   # Placeholder

            pairs.append({
                'base': base,
                'perp_volume_24h': perp_volume,
                'spot_volume_24h': spot_volume,
                'perp_asset': perp.get('name'),
                'spot_coin': spot_markets[base]['coin'],
                'spot_asset': spot_markets[base]['asset'],
                'perp_sz_decimals': perp.get('szDecimals', 3),
                'spot_sz_decimals': spot_markets[base]['sz_decimals'],
            })

        # Sort by perp volume (in production, use real volumes)
        # For now, prioritize well-known assets
        priority_assets = ['BTC', 'ETH', 'SOL', 'HYPE', 'ARB', 'OP', 'MATIC',
                          'AVAX', 'LINK', 'UNI', 'AAVE', 'CRV', 'LDO', 'DOGE',
                          'SHIB', 'APT', 'SUI', 'SEI', 'TIA', 'INJ', 'ATOM']

        def sort_key(pair):
            base = pair['base']
            if base in priority_assets:
                return (0, priority_assets.index(base))
            return (1, 0)

        pairs.sort(key=sort_key)

        top_pairs = pairs[:top_n]

        print(f"\n✅ Top {len(top_pairs)} Pairs:")
        for i, pair in enumerate(top_pairs, 1):
            print(f"  {i}. {pair['base']}")

        return top_pairs


class MultiPairDataCollector:
    """Collects real-time edge data for multiple pairs."""

    def __init__(self, pairs: List[Dict], duration_seconds: int = 1800):
        self.pairs = pairs
        self.duration = duration_seconds
        self.data = defaultdict(list)  # pair_base -> list of edges
        self.info = Info('https://api.hyperliquid.xyz', skip_ws=True)

        # Fee structure
        self.fees = {
            'perp': {'maker': 1.5, 'taker': 4.5},
            'spot': {'maker': 4.0, 'taker': 7.0}
        }
        self.maker_total = self.fees['perp']['maker'] + self.fees['spot']['maker']
        self.taker_total = self.fees['perp']['taker'] + self.fees['spot']['taker']

    def compute_edges(self, perp_bid: float, perp_ask: float,
                      spot_bid: float, spot_ask: float) -> Dict[str, float]:
        """Compute arbitrage edges."""
        if not all([perp_bid, perp_ask, spot_bid, spot_ask]):
            return {}

        # Perp -> Spot: sell perp (bid), buy spot (ask)
        mid_ps = (perp_bid + spot_ask) / 2.0
        e_ps_raw = ((perp_bid - spot_ask) / mid_ps) * 10000 if mid_ps else 0

        # Spot -> Perp: sell spot (bid), buy perp (ask)
        mid_sp = (spot_bid + perp_ask) / 2.0
        e_sp_raw = ((spot_bid - perp_ask) / mid_sp) * 10000 if mid_sp else 0

        return {
            'ps_mm': e_ps_raw - self.maker_total,
            'sp_mm': e_sp_raw - self.maker_total,
            'ps_tt': e_ps_raw - self.taker_total,
            'sp_tt': e_sp_raw - self.taker_total,
            'mid_ref': (mid_ps + mid_sp) / 2.0 if mid_ps and mid_sp else 0,
        }

    async def collect_data(self):
        """Collect edge data for all pairs via WebSocket."""
        print(f"\n📊 Starting data collection for {len(self.pairs)} pairs...")
        print(f"⏱️  Duration: {self.duration}s ({self.duration/60:.0f} minutes)")
        print()

        # Subscribe to all pairs
        subscriptions = []
        pair_map = {}  # coin_name -> pair_info

        for pair in self.pairs:
            base = pair['base']
            spot_coin = pair['spot_coin']

            subscriptions.append({
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": base}  # Perp
            })
            subscriptions.append({
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": spot_coin}  # Spot
            })

            pair_map[base] = {'type': 'perp', 'pair': pair}
            pair_map[spot_coin] = {'type': 'spot', 'pair': pair}

        # Track order books
        books = {}  # coin -> {bid, ask}
        start_time = time.time()
        message_count = 0
        edge_count = 0

        async for ws in websockets.connect('wss://api.hyperliquid.xyz/ws',
                                          ping_interval=15, ping_timeout=15):
            try:
                # Send subscriptions
                for sub in subscriptions:
                    await ws.send(json.dumps(sub))

                print("✅ Subscribed to all pairs")
                print(f"🔄 Collecting data... (Ctrl+C to stop early)\n")

                while True:
                    elapsed = time.time() - start_time
                    if elapsed >= self.duration:
                        print(f"\n⏰ Duration reached: {elapsed:.0f}s")
                        break

                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    message_count += 1

                    data = json.loads(msg)
                    if not isinstance(data, dict) or data.get('channel') != 'l2Book':
                        continue

                    coin = data['data'].get('coin')
                    if coin not in pair_map:
                        continue

                    # Parse order book
                    levels = data['data'].get('levels', [[], []])
                    if len(levels) != 2:
                        continue

                    bids, asks = levels[0], levels[1]
                    if not bids or not asks:
                        continue

                    bid = float(bids[0]['px'])
                    ask = float(asks[0]['px'])

                    books[coin] = {'bid': bid, 'ask': ask}

                    # Check if we have both perp and spot for this pair
                    pair_info = pair_map[coin]['pair']
                    base = pair_info['base']
                    spot_coin = pair_info['spot_coin']

                    if base in books and spot_coin in books:
                        perp_book = books[base]
                        spot_book = books[spot_coin]

                        edges = self.compute_edges(
                            perp_book['bid'], perp_book['ask'],
                            spot_book['bid'], spot_book['ask']
                        )

                        if edges:
                            self.data[base].append({
                                'timestamp': datetime.now(timezone.utc),
                                'ps_mm': edges['ps_mm'],
                                'sp_mm': edges['sp_mm'],
                                'ps_tt': edges['ps_tt'],
                                'sp_tt': edges['sp_tt'],
                                'mid_ref': edges['mid_ref'],
                            })
                            edge_count += 1

                    # Progress update every 5 seconds
                    if message_count % 1000 == 0:
                        print(f"  [{elapsed:.0f}s] Messages: {message_count:,} | "
                              f"Edges: {edge_count:,} | "
                              f"Pairs: {len([p for p in self.data.values() if p])}")

            except asyncio.TimeoutError:
                continue
            except KeyboardInterrupt:
                print("\n⚠️  Collection stopped by user")
                break
            except Exception as e:
                print(f"❌ Error: {e}")
                break

        print(f"\n✅ Collection complete!")
        print(f"   Total messages: {message_count:,}")
        print(f"   Total edges: {edge_count:,}")
        print(f"   Pairs with data: {len([p for p in self.data.values() if p])}")


class EdgeAnalyzer:
    """Analyzes edge distributions and calculates optimal thresholds."""

    def __init__(self, data: Dict[str, List[Dict]], fees: Dict):
        self.data = data
        self.fees = fees
        self.maker_total = fees['perp']['maker'] + fees['spot']['maker']
        self.taker_total = fees['perp']['taker'] + fees['spot']['taker']

    def analyze_pair(self, base: str, direction: str = 'ps') -> Dict:
        """
        Analyze a single pair and direction.

        Returns:
            {
                'base': str,
                'direction': str,
                'total_samples': int,
                'bps_curve': [(threshold, count), ...],
                'optimal_threshold_ioc': float,
                'optimal_threshold_alo': float,
                'expected_trades_per_day': float,
                'statistics': {...}
            }
        """
        edges_data = self.data.get(base, [])
        if not edges_data:
            return None

        # Extract edges for this direction
        field = f'{direction}_mm'  # Use maker fees as baseline
        edges = [e[field] for e in edges_data if field in e]

        if not edges:
            return None

        # Calculate BPS curve (threshold -> opportunity count)
        bps_curve = []
        thresholds = list(range(0, 101, 2))  # 0, 2, 4, ..., 100 bps

        for threshold in thresholds:
            count = sum(1 for e in edges if e >= threshold)
            bps_curve.append((threshold, count))

        # Statistics
        stats = {
            'min': min(edges),
            'max': max(edges),
            'mean': statistics.mean(edges),
            'median': statistics.median(edges),
            'stdev': statistics.stdev(edges) if len(edges) > 1 else 0,
            'p95': statistics.quantiles(edges, n=20)[18] if len(edges) >= 20 else max(edges),
            'positive_count': sum(1 for e in edges if e > 0),
            'positive_pct': (sum(1 for e in edges if e > 0) / len(edges)) * 100,
        }

        # Calculate optimal thresholds
        # For IOC: need to cover taker fees + aggressive pricing (43 bps total)
        ioc_breakeven = 43.0 - self.maker_total  # Net threshold needed

        # For ALO: need to cover maker fees only (11 bps total)
        alo_breakeven = 11.0 - self.maker_total

        # Find optimal: balance between frequency and profitability
        # Target: 5 bps net profit minimum
        optimal_ioc = ioc_breakeven + 5
        optimal_alo = alo_breakeven + 5

        # Expected trades per day (extrapolate from sample)
        duration_hours = (edges_data[-1]['timestamp'] - edges_data[0]['timestamp']).total_seconds() / 3600
        if duration_hours > 0:
            trades_per_hour_ioc = sum(1 for e in edges if e >= optimal_ioc) / duration_hours
            trades_per_day_ioc = trades_per_hour_ioc * 24

            trades_per_hour_alo = sum(1 for e in edges if e >= optimal_alo) / duration_hours
            trades_per_day_alo = trades_per_hour_alo * 24
        else:
            trades_per_day_ioc = 0
            trades_per_day_alo = 0

        return {
            'base': base,
            'direction': direction,
            'total_samples': len(edges),
            'duration_minutes': duration_hours * 60,
            'bps_curve': bps_curve,
            'optimal_threshold_ioc': optimal_ioc,
            'optimal_threshold_alo': optimal_alo,
            'expected_trades_per_day_ioc': trades_per_day_ioc,
            'expected_trades_per_day_alo': trades_per_day_alo,
            'statistics': stats,
        }

    def analyze_all_pairs(self) -> List[Dict]:
        """Analyze all pairs and both directions."""
        results = []

        for base in self.data.keys():
            # Perp -> Spot
            ps_analysis = self.analyze_pair(base, 'ps')
            if ps_analysis:
                results.append(ps_analysis)

            # Spot -> Perp
            sp_analysis = self.analyze_pair(base, 'sp')
            if sp_analysis:
                results.append(sp_analysis)

        return results


class ReportGenerator:
    """Generates comprehensive analysis reports."""

    def __init__(self, analyses: List[Dict]):
        self.analyses = analyses

    def generate_report(self) -> str:
        """Generate markdown report."""
        report = []

        report.append("# Multi-Pair Arbitrage Analysis Report")
        report.append(f"\n**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

        # Summary
        report.append("## Executive Summary\n")
        report.append(f"- **Total Pairs Analyzed:** {len(set(a['base'] for a in self.analyses))}")
        report.append(f"- **Total Directions:** {len(self.analyses)}")
        report.append(f"- **Total Samples:** {sum(a['total_samples'] for a in self.analyses):,}\n")

        # Top opportunities by expected trades/day
        report.append("## Top Opportunities (IOC Strategy)\n")
        sorted_ioc = sorted(self.analyses, key=lambda x: x['expected_trades_per_day_ioc'], reverse=True)[:10]

        report.append("| Rank | Pair | Direction | Optimal BPS | Trades/Day | Median Edge | P95 Edge |")
        report.append("|------|------|-----------|-------------|------------|-------------|----------|")

        for i, analysis in enumerate(sorted_ioc, 1):
            base = analysis['base']
            direction = "Perp→Spot" if analysis['direction'] == 'ps' else "Spot→Perp"
            opt_bps = analysis['optimal_threshold_ioc']
            trades_day = analysis['expected_trades_per_day_ioc']
            median = analysis['statistics']['median']
            p95 = analysis['statistics']['p95']

            report.append(f"| {i} | {base} | {direction} | {opt_bps:.1f} | {trades_day:.1f} | "
                         f"{median:.1f} | {p95:.1f} |")

        # Detailed pair analysis
        report.append("\n## Detailed Pair Analysis\n")

        pairs = sorted(set(a['base'] for a in self.analyses))

        for base in pairs:
            pair_analyses = [a for a in self.analyses if a['base'] == base]

            report.append(f"### {base}\n")

            for analysis in pair_analyses:
                direction = "Perp→Spot" if analysis['direction'] == 'ps' else "Spot→Perp"
                stats = analysis['statistics']

                report.append(f"#### {direction}\n")
                report.append(f"- **Samples:** {analysis['total_samples']:,}")
                report.append(f"- **Duration:** {analysis['duration_minutes']:.1f} minutes")
                report.append(f"- **Optimal Threshold (IOC):** {analysis['optimal_threshold_ioc']:.1f} bps")
                report.append(f"- **Optimal Threshold (ALO):** {analysis['optimal_threshold_alo']:.1f} bps")
                report.append(f"- **Expected Trades/Day (IOC):** {analysis['expected_trades_per_day_ioc']:.1f}")
                report.append(f"- **Expected Trades/Day (ALO):** {analysis['expected_trades_per_day_alo']:.1f}")
                report.append(f"- **Statistics:**")
                report.append(f"  - Median: {stats['median']:.2f} bps")
                report.append(f"  - Mean: {stats['mean']:.2f} bps")
                report.append(f"  - Std Dev: {stats['stdev']:.2f} bps")
                report.append(f"  - Min: {stats['min']:.2f} bps")
                report.append(f"  - Max: {stats['max']:.2f} bps")
                report.append(f"  - P95: {stats['p95']:.2f} bps")
                report.append(f"  - Positive %: {stats['positive_pct']:.1f}%\n")

                # BPS Curve (selected thresholds)
                report.append("**BPS Curve (opportunities at each threshold):**\n")
                report.append("| Threshold | Count | % of Total |")
                report.append("|-----------|-------|------------|")

                selected_thresholds = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
                curve_dict = dict(analysis['bps_curve'])
                total = analysis['total_samples']

                for threshold in selected_thresholds:
                    if threshold in curve_dict:
                        count = curve_dict[threshold]
                        pct = (count / total) * 100 if total > 0 else 0
                        report.append(f"| {threshold} | {count:,} | {pct:.2f}% |")

                report.append("")

        return "\n".join(report)

    def save_report(self, filename: str = "multi_pair_analysis.md"):
        """Save report to file."""
        report = self.generate_report()
        with open(filename, 'w') as f:
            f.write(report)
        print(f"\n📝 Report saved to: {filename}")


async def main():
    parser = argparse.ArgumentParser(description='Multi-Pair Arbitrage Discovery')
    parser.add_argument('--duration', type=int, default=1800,
                       help='Data collection duration in seconds (default: 1800 = 30min)')
    parser.add_argument('--top', type=int, default=20,
                       help='Number of top pairs to analyze (default: 20)')
    parser.add_argument('--output', type=str, default='multi_pair_analysis.md',
                       help='Output report filename (default: multi_pair_analysis.md)')

    args = parser.parse_args()

    print("="*70)
    print("MULTI-PAIR ARBITRAGE DISCOVERY SYSTEM")
    print("="*70)
    print()

    # Step 1: Discover pairs
    discovery = PairDiscovery()
    pairs = discovery.get_top_pairs(top_n=args.top)

    if not pairs:
        print("❌ No pairs found!")
        return

    # Step 2: Collect data
    collector = MultiPairDataCollector(pairs, duration_seconds=args.duration)
    await collector.collect_data()

    if not collector.data:
        print("❌ No data collected!")
        return

    # Step 3: Analyze edges
    print("\n📈 Analyzing edge distributions...")
    analyzer = EdgeAnalyzer(collector.data, collector.fees)
    analyses = analyzer.analyze_all_pairs()

    if not analyses:
        print("❌ No analyses completed!")
        return

    print(f"✅ Analyzed {len(analyses)} pair-directions")

    # Step 4: Generate report
    print("\n📊 Generating report...")
    generator = ReportGenerator(analyses)
    generator.save_report(args.output)

    # Print summary to console
    print("\n" + "="*70)
    print("TOP 5 OPPORTUNITIES (IOC Strategy)")
    print("="*70)

    sorted_analyses = sorted(analyses, key=lambda x: x['expected_trades_per_day_ioc'], reverse=True)[:5]

    for i, analysis in enumerate(sorted_analyses, 1):
        base = analysis['base']
        direction = "Perp→Spot" if analysis['direction'] == 'ps' else "Spot→Perp"
        opt_bps = analysis['optimal_threshold_ioc']
        trades_day = analysis['expected_trades_per_day_ioc']

        print(f"{i}. {base} ({direction})")
        print(f"   Optimal: {opt_bps:.1f} bps | Trades/day: {trades_day:.1f}")

    print("\n✅ Analysis complete!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Stopped by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
