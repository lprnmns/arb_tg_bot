#!/usr/bin/env python3
"""
Historical Volatility Analysis
===============================

Analyze edges database around trade execution times to determine
which side (PERP or SPOT) was more volatile when spreads spiked.
"""

import sys
sys.path.insert(0, '/app')

from bot.storage import pg_conn

print("=" * 80)
print("📊 HISTORICAL VOLATILITY ANALYSIS")
print("=" * 80)
print()

with pg_conn() as conn:
    cur = conn.cursor()

    # Get all successful trades
    cur.execute("""
        SELECT
            id,
            ts,
            direction,
            mm_best_bps,
            notional_usd
        FROM trades
        WHERE status = 'POSTED'
            AND ts > NOW() - INTERVAL '7 days'
        ORDER BY ts DESC
        LIMIT 50
    """)

    trades = cur.fetchall()

    print(f"Analyzing {len(trades)} recent trades...")
    print()

    perp_driven_count = 0
    spot_driven_count = 0
    both_driven_count = 0
    insufficient_data = 0

    for trade in trades:
        trade_id, trade_ts, direction, edge_bps, notional = trade

        # Get edges around trade time (±5 seconds)
        cur.execute("""
            SELECT
                ts,
                edge_ps_mm_bps,
                edge_sp_mm_bps,
                mid_ref
            FROM edges
            WHERE ts BETWEEN %s::timestamp - INTERVAL '5 seconds'
                         AND %s::timestamp + INTERVAL '5 seconds'
            ORDER BY ts ASC
        """, (trade_ts, trade_ts))

        edges = cur.fetchall()

        if len(edges) < 10:
            insufficient_data += 1
            continue

        # Calculate baseline (first 5 ticks)
        baseline_edges = edges[:5]
        ps_baseline = sum([e[1] for e in baseline_edges]) / len(baseline_edges)
        sp_baseline = sum([e[2] for e in baseline_edges]) / len(baseline_edges)

        # Find spike (max edge)
        spike_edges = edges[5:]
        if direction == "perp->spot":
            # Find max PS edge
            max_edge = max([e[1] for e in spike_edges])
            spike_tick = [e for e in spike_edges if e[1] == max_edge][0]
        else:
            # Find max SP edge
            max_edge = max([e[2] for e in spike_edges])
            spike_tick = [e for e in spike_edges if e[2] == max_edge][0]

        # Calculate movement from baseline
        ps_movement = abs(spike_tick[1] - ps_baseline)
        sp_movement = abs(spike_tick[2] - sp_baseline)

        # Classify
        if ps_movement > sp_movement * 1.5:
            source = "PERP"
            perp_driven_count += 1
        elif sp_movement > ps_movement * 1.5:
            source = "SPOT"
            spot_driven_count += 1
        else:
            source = "BOTH"
            both_driven_count += 1

        print(f"Trade {trade_id}: {direction} @ {edge_bps:.1f} bps → {source}")
        print(f"   PS movement: {ps_movement:.2f} bps | SP movement: {sp_movement:.2f} bps")

    print()
    print("=" * 80)
    print("📊 RESULTS")
    print("=" * 80)

    total = perp_driven_count + spot_driven_count + both_driven_count
    if total > 0:
        print(f"Total analyzed: {total}")
        print(f"Insufficient data: {insufficient_data}")
        print()
        print(f"🔴 PERP-driven: {perp_driven_count} ({perp_driven_count/total*100:.1f}%)")
        print(f"🔵 SPOT-driven: {spot_driven_count} ({spot_driven_count/total*100:.1f}%)")
        print(f"🟣 BOTH-driven: {both_driven_count} ({both_driven_count/total*100:.1f}%)")
        print()

        if perp_driven_count / total > 0.7:
            print("✅ CONCLUSION: PERP is the volatile side!")
            print("💡 RECOMMENDATION: Use IOC perp + ALO spot")
        elif spot_driven_count / total > 0.7:
            print("⚠️  CONCLUSION: SPOT is the volatile side!")
            print("💡 RECOMMENDATION: Keep IOC both")
        else:
            print("⚠️  CONCLUSION: Mixed volatility")
            print("💡 RECOMMENDATION: Need more data")
    else:
        print("❌ No trades analyzed (insufficient edge data)")

    print("=" * 80)

    cur.close()
