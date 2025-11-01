#!/usr/bin/env python3
"""
Quick DB-based anomaly analysis
"""
import psycopg2
import numpy as np
from datetime import datetime, timedelta

# Connect to DB
conn = psycopg2.connect(
    host="localhost",
    port=5432,
    dbname="hl_arb",
    user="hluser",
    password="hlpass123"
)

cur = conn.cursor()

# Get last hour of edge data
cur.execute("""
    SELECT
        ts,
        edge_ps_mm_bps,
        edge_sp_mm_bps
    FROM edges
    WHERE ts > NOW() - INTERVAL '1 hour'
    ORDER BY ts ASC
""")

rows = cur.fetchall()

if len(rows) < 100:
    print("⚠️  Not enough data. Need at least 100 edge records.")
    exit(1)

print(f"📊 Analyzing {len(rows)} edge records from last hour...")
print()

# Convert to numpy arrays
edge_ps = np.array([r[1] for r in rows])
edge_sp = np.array([r[2] for r in rows])

# Calculate statistics
ps_mean = np.mean(edge_ps)
ps_std = np.std(edge_ps)
ps_min = np.min(edge_ps)
ps_max = np.max(edge_ps)

sp_mean = np.mean(edge_sp)
sp_std = np.std(edge_sp)
sp_min = np.min(edge_sp)
sp_max = np.max(edge_sp)

# Calculate correlation
correlation = np.corrcoef(edge_ps, edge_sp)[0, 1]

print("=" * 70)
print("📈 EDGE STATISTICS")
print("=" * 70)
print(f"perp->spot (PS) edge:")
print(f"   Mean: {ps_mean:.2f} bps")
print(f"   StdDev: {ps_std:.2f} bps  👈 Volatility measure")
print(f"   Range: {ps_min:.2f} to {ps_max:.2f} bps")
print()
print(f"spot->perp (SP) edge:")
print(f"   Mean: {sp_mean:.2f} bps")
print(f"   StdDev: {sp_std:.2f} bps  👈 Volatility measure")
print(f"   Range: {sp_min:.2f} to {sp_max:.2f} bps")
print()
print(f"📊 Correlation: {correlation:.3f}")
print()

# Find anomalies (>15 bps)
ps_anomalies = np.abs(edge_ps) > 15
sp_anomalies = np.abs(edge_sp) > 15

ps_anomaly_count = np.sum(ps_anomalies)
sp_anomaly_count = np.sum(sp_anomalies)

print("=" * 70)
print("🚨 ANOMALIES (>15 bps)")
print("=" * 70)
print(f"PS edge >15 bps: {ps_anomaly_count} times ({ps_anomaly_count/len(rows)*100:.1f}%)")
print(f"SP edge >15 bps: {sp_anomaly_count} times ({sp_anomaly_count/len(rows)*100:.1f}%)")
print()

# Volatility ratio
volatility_ratio = ps_std / sp_std if sp_std > 0 else 0

print("=" * 70)
print("🔬 VOLATILITY ANALYSIS")
print("=" * 70)
print(f"PS/SP Volatility Ratio: {volatility_ratio:.2f}")
print()

if volatility_ratio > 1.5:
    print("✅ PS edge is MORE volatile (perp->spot direction)")
    print("   This suggests: SPOT price is more stable")
    print()
    print("💡 RECOMMENDATION:")
    print("   ✅ Use IOC for PERP (catches volatile side)")
    print("   ✅ Use ALO for SPOT (stable side, save fees!)")
    print(f"   💰 Opening cost: 8.5 bps (4.5 perp + 4.0 spot)")
    print(f"   💰 Total cost: ~14 bps (vs 18.2 bps current)")
    print(f"   🚀 Profit boost: +29% at 20 bps threshold!")
elif volatility_ratio < 0.67:
    print("✅ SP edge is MORE volatile (spot->perp direction)")
    print("   This suggests: PERP price is more stable")
    print()
    print("💡 RECOMMENDATION:")
    print("   ✅ Use ALO for PERP (stable side, save fees!)")
    print("   ✅ Use IOC for SPOT (catches volatile side)")
else:
    print("⚠️  BOTH edges have similar volatility")
    print("   This suggests: Both PERP and SPOT are volatile")
    print()
    print("💡 RECOMMENDATION:")
    print("   ✅ Keep current strategy: IOC for both")
    print("   ⚠️  ALO might not work well (both sides moving)")

print("=" * 70)
print()

# Check correlation insight
print("=" * 70)
print("🧪 CORRELATION INSIGHT")
print("=" * 70)
if correlation < -0.5:
    print(f"Strong NEGATIVE correlation ({correlation:.2f})")
    print("   → When one edge increases, other decreases")
    print("   → Suggests: Single-sided price movements")
    print("   → This is GOOD for hybrid ALO/IOC strategy!")
elif correlation > 0.5:
    print(f"Strong POSITIVE correlation ({correlation:.2f})")
    print("   → Both edges move together")
    print("   → Suggests: Synchronized price movements")
    print("   → ALO might be risky (both sides moving)")
else:
    print(f"Weak correlation ({correlation:.2f})")
    print("   → No clear pattern")

print("=" * 70)

cur.close()
conn.close()
