# 🎯 OPPORTUNITY TRACKER - IMPLEMENTATION PLAN

## 📋 OBJECTIVE

Create a **non-intrusive** data collection system that:
- ✅ Runs alongside main bot (20 bps threshold)
- ✅ Tracks ALL opportunities ≥10 bps
- ✅ Records detailed volatility analysis
- ✅ Measures latency at every step
- ✅ Provides Telegram monitoring
- ✅ **DOES NOT EXECUTE TRADES** (data collection only)
- ✅ **DOES NOT AFFECT MAIN BOT** (isolated system)

---

## 🏗️ ARCHITECTURE

```
Main Bot (20 bps)           Opportunity Tracker (10 bps)
─────────────────           ────────────────────────────
                                        │
strategy.on_edge()                      │
    │                                   │
    ├─→ Check threshold (20 bps)       │
    │   ├─ YES → Execute trade         │
    │   └─ NO → Skip                   │
    │                                   │
    └─→ ALWAYS call tracker ────────→  │
                                        │
                                   tracker.on_edge()
                                        │
                                   ├─→ Check 10+ bps?
                                   │   ├─ NO → Skip
                                   │   └─ YES ↓
                                   │
                                   ├─→ Analyze volatility
                                   │   ├─ PERP movement
                                   │   ├─ SPOT movement
                                   │   └─ Source classification
                                   │
                                   ├─→ Simulate strategies
                                   │   ├─ IOC both (current)
                                   │   ├─ IOC+ALO hybrid
                                   │   └─ Expected costs
                                   │
                                   └─→ Save to DB
                                       └─ opportunities table
```

---

## 📊 DATA MODEL

### Table: `opportunities`

```sql
CREATE TABLE opportunities (
    id BIGSERIAL PRIMARY KEY,

    -- Timing
    detected_at TIMESTAMP WITH TIME ZONE NOT NULL,
    detection_latency_ms INTEGER,  -- Time from edge calculation to detection

    -- Market data
    edge_bps DOUBLE PRECISION NOT NULL,
    perp_bid DOUBLE PRECISION NOT NULL,
    perp_ask DOUBLE PRECISION NOT NULL,
    spot_bid DOUBLE PRECISION NOT NULL,
    spot_ask DOUBLE PRECISION NOT NULL,
    mid_ref DOUBLE PRECISION NOT NULL,

    -- Baseline (20-tick rolling average)
    baseline_perp_bid DOUBLE PRECISION,
    baseline_perp_ask DOUBLE PRECISION,
    baseline_spot_bid DOUBLE PRECISION,
    baseline_spot_ask DOUBLE PRECISION,

    -- Volatility analysis
    perp_bid_deviation_bps DOUBLE PRECISION,
    perp_ask_deviation_bps DOUBLE PRECISION,
    spot_bid_deviation_bps DOUBLE PRECISION,
    spot_ask_deviation_bps DOUBLE PRECISION,
    perp_movement_bps DOUBLE PRECISION,  -- abs(perp_ask deviation)
    spot_movement_bps DOUBLE PRECISION,  -- abs(spot_bid deviation)

    -- Classification
    volatility_source TEXT NOT NULL,  -- 'PERP', 'SPOT', or 'BOTH'
    volatility_ratio DOUBLE PRECISION,  -- perp_movement / spot_movement

    -- Strategy simulation
    would_trade_current BOOLEAN,  -- Would main bot (20 bps) trade this?

    -- Cost projections (if we traded)
    cost_ioc_both DOUBLE PRECISION,      -- 11.5 bps open + 6.7 close = 18.2
    cost_ioc_perp_alo_spot DOUBLE PRECISION,  -- 8.5 bps open + 5.5 close = 14.0
    cost_alo_both DOUBLE PRECISION,      -- 5.5 bps open + 5.5 close = 11.0

    -- Expected outcomes
    expected_profit_ioc DOUBLE PRECISION,  -- edge - cost_ioc_both
    expected_profit_hybrid DOUBLE PRECISION,  -- edge - cost_ioc_perp_alo_spot

    -- Metadata
    analysis_duration_ms INTEGER,  -- Time taken for full analysis

    -- Indexes
    INDEX idx_detected_at (detected_at),
    INDEX idx_volatility_source (volatility_source),
    INDEX idx_edge_bps (edge_bps)
);
```

---

## 🔧 IMPLEMENTATION COMPONENTS

### 1. `OpportunityTracker` Class

**File:** `bot/opportunity_tracker.py`

```python
class OpportunityTracker:
    """
    Non-intrusive opportunity tracking for data collection.

    - Monitors all edges ≥10 bps
    - Analyzes volatility source (PERP vs SPOT)
    - Simulates different strategies
    - Records detailed metrics to DB
    - NO ACTUAL TRADING
    """

    def __init__(self, threshold_bps=10, baseline_window=20):
        self.threshold_bps = threshold_bps
        self.baseline = RollingBaseline(window=baseline_window)
        self.opportunities_count = 0
        self.start_time = time.time()

    def on_edge(self, edge_bps, perp_bid, perp_ask, spot_bid, spot_ask,
                recv_ms, send_ms, would_main_bot_trade):
        """
        Called on EVERY edge update from strategy.py

        Args:
            edge_bps: Current spread (perp->spot)
            perp_bid, perp_ask, spot_bid, spot_ask: Market prices
            recv_ms, send_ms: Timing data
            would_main_bot_trade: Did main bot (20 bps) trade this?
        """
        start_analysis = time.time()

        # Update baseline (always, even if below threshold)
        self.baseline.update(perp_bid, perp_ask, spot_bid, spot_ask)

        # Check threshold
        if edge_bps < self.threshold_bps:
            return  # Not interesting, skip

        # ─────────────────────────────────────────────────
        # OPPORTUNITY DETECTED! Analyze in detail
        # ─────────────────────────────────────────────────

        detection_latency = (send_ms - recv_ms) if recv_ms and send_ms else None

        # Get baseline
        baseline = self.baseline.get()
        if not baseline:
            return  # Need baseline first

        # Calculate deviations
        mid = (perp_bid + perp_ask + spot_bid + spot_ask) / 4

        perp_bid_dev = (perp_bid - baseline['perp_bid']) / mid * 10000
        perp_ask_dev = (perp_ask - baseline['perp_ask']) / mid * 10000
        spot_bid_dev = (spot_bid - baseline['spot_bid']) / mid * 10000
        spot_ask_dev = (spot_ask - baseline['spot_ask']) / mid * 10000

        # Calculate movements (for classification)
        perp_movement = abs(perp_ask_dev)  # perp->spot: care about ask
        spot_movement = abs(spot_bid_dev)  # perp->spot: care about bid

        # Classify source
        source, ratio = self._classify_source(perp_movement, spot_movement)

        # Simulate strategies
        costs = self._simulate_strategies()

        # Expected profits
        profit_ioc = edge_bps - costs['ioc_both']
        profit_hybrid = edge_bps - costs['ioc_perp_alo_spot']

        # Analysis duration
        analysis_ms = (time.time() - start_analysis) * 1000

        # Save to DB
        opportunity = {
            'detected_at': datetime.now(timezone.utc),
            'detection_latency_ms': detection_latency,
            'edge_bps': edge_bps,
            'perp_bid': perp_bid,
            'perp_ask': perp_ask,
            'spot_bid': spot_bid,
            'spot_ask': spot_ask,
            'mid_ref': mid,
            'baseline_perp_bid': baseline['perp_bid'],
            'baseline_perp_ask': baseline['perp_ask'],
            'baseline_spot_bid': baseline['spot_bid'],
            'baseline_spot_ask': baseline['spot_ask'],
            'perp_bid_deviation_bps': perp_bid_dev,
            'perp_ask_deviation_bps': perp_ask_dev,
            'spot_bid_deviation_bps': spot_bid_dev,
            'spot_ask_deviation_bps': spot_ask_dev,
            'perp_movement_bps': perp_movement,
            'spot_movement_bps': spot_movement,
            'volatility_source': source,
            'volatility_ratio': ratio,
            'would_trade_current': would_main_bot_trade,
            'cost_ioc_both': costs['ioc_both'],
            'cost_ioc_perp_alo_spot': costs['ioc_perp_alo_spot'],
            'cost_alo_both': costs['alo_both'],
            'expected_profit_ioc': profit_ioc,
            'expected_profit_hybrid': profit_hybrid,
            'analysis_duration_ms': analysis_ms
        }

        self._save_opportunity(opportunity)
        self.opportunities_count += 1

    def _classify_source(self, perp_movement, spot_movement):
        """Classify volatility source"""
        if perp_movement < 0.1 and spot_movement < 0.1:
            return 'BOTH', 1.0  # Both very small

        ratio = perp_movement / max(spot_movement, 0.01)

        if ratio > 1.5:
            return 'PERP', ratio
        elif ratio < 0.67:
            return 'SPOT', ratio
        else:
            return 'BOTH', ratio

    def _simulate_strategies(self):
        """Calculate expected costs for different strategies"""
        return {
            'ioc_both': 18.2,  # 11.5 open + 6.7 close (80% ALO)
            'ioc_perp_alo_spot': 14.0,  # 8.5 open + 5.5 close (80% ALO)
            'alo_both': 11.0  # 5.5 open + 5.5 close (100% ALO - theoretical)
        }

    def _save_opportunity(self, opp):
        """Save to database (async, non-blocking)"""
        # Use async insert to avoid blocking main bot
        asyncio.create_task(self._async_insert(opp))

    async def _async_insert(self, opp):
        """Async DB insert"""
        # Implementation in storage module
        pass

    def get_stats(self):
        """Get current stats for Telegram commands"""
        return {
            'total_opportunities': self.opportunities_count,
            'uptime_hours': (time.time() - self.start_time) / 3600,
            'opportunities_per_hour': self.opportunities_count / ((time.time() - self.start_time) / 3600)
        }
```

### 2. `RollingBaseline` Helper

**File:** `bot/opportunity_tracker.py` (same file)

```python
class RollingBaseline:
    """
    Maintains rolling average of last N ticks for baseline comparison
    """

    def __init__(self, window=20):
        self.window = window
        self.perp_bids = deque(maxlen=window)
        self.perp_asks = deque(maxlen=window)
        self.spot_bids = deque(maxlen=window)
        self.spot_asks = deque(maxlen=window)

    def update(self, perp_bid, perp_ask, spot_bid, spot_ask):
        """Add new tick"""
        self.perp_bids.append(perp_bid)
        self.perp_asks.append(perp_ask)
        self.spot_bids.append(spot_bid)
        self.spot_asks.append(spot_ask)

    def get(self):
        """Get current baseline (None if not enough data)"""
        if len(self.perp_bids) < self.window:
            return None

        return {
            'perp_bid': statistics.mean(self.perp_bids),
            'perp_ask': statistics.mean(self.perp_asks),
            'spot_bid': statistics.mean(self.spot_bids),
            'spot_ask': statistics.mean(self.spot_asks)
        }
```

---

## 🔗 INTEGRATION INTO MAIN BOT

### Modify `strategy.py`

**CRITICAL: NON-BREAKING CHANGES ONLY!**

```python
# At top of file
from .opportunity_tracker import OpportunityTracker

class Strategy:
    def __init__(self, ...):
        # ... existing code ...

        # ✅ NEW: Add opportunity tracker (NON-INTRUSIVE)
        self.opportunity_tracker = OpportunityTracker(
            threshold_bps=10,  # Lower than main bot (20 bps)
            baseline_window=20
        )

    async def on_edge(self, ...):
        # ... existing edge calculation ...

        # ✅ NEW: Track opportunity (BEFORE main bot logic)
        # This runs on EVERY edge, regardless of threshold
        try:
            would_trade = mm_best >= settings.threshold_bps  # Main bot would trade?

            self.opportunity_tracker.on_edge(
                edge_bps=mm_best,
                perp_bid=pbid,
                perp_ask=pask,
                spot_bid=sbid,
                spot_ask=sask,
                recv_ms=recv_ms,
                send_ms=send_ms,
                would_main_bot_trade=would_trade
            )
        except Exception as e:
            # ✅ CRITICAL: Never let tracker crash main bot!
            print(f"⚠️  Opportunity tracker error (non-fatal): {e}")

        # ... existing main bot logic (UNCHANGED) ...
        if mm_best < settings.threshold_bps:
            return  # Main bot skips

        # ... existing trade execution (UNCHANGED) ...
```

**Key principles:**
- ✅ Tracker runs BEFORE main bot logic
- ✅ Wrapped in try/except (can't crash main bot)
- ✅ Async/non-blocking DB writes
- ✅ Main bot logic UNCHANGED
- ✅ If tracker fails, main bot continues normally

---

## 📱 TELEGRAM MONITORING

### New Commands

**File:** `bot/telegram_bot.py`

```python
# Add these handlers

async def cmd_test_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /test_stats - Show opportunity tracker statistics
    """
    tracker = strategy.opportunity_tracker  # Get from global strategy
    stats = tracker.get_stats()

    # Query DB for detailed stats
    with pg_conn() as conn:
        cur = conn.cursor()

        # Total opportunities
        cur.execute("SELECT COUNT(*) FROM opportunities")
        total = cur.fetchone()[0]

        # By source
        cur.execute("""
            SELECT
                volatility_source,
                COUNT(*) as count,
                AVG(edge_bps) as avg_edge,
                AVG(perp_movement_bps) as avg_perp_mov,
                AVG(spot_movement_bps) as avg_spot_mov
            FROM opportunities
            GROUP BY volatility_source
        """)
        by_source = cur.fetchall()

        # Recent (last hour)
        cur.execute("""
            SELECT COUNT(*)
            FROM opportunities
            WHERE detected_at > NOW() - INTERVAL '1 hour'
        """)
        last_hour = cur.fetchone()[0]

    msg = f"""
📊 <b>OPPORTUNITY TRACKER STATS</b>

🎯 <b>Collection Status:</b>
  Total opportunities: {total}
  Last hour: {last_hour}
  Uptime: {stats['uptime_hours']:.1f} hours
  Rate: {stats['opportunities_per_hour']:.1f}/hour

📈 <b>By Volatility Source:</b>
"""

    for source, count, avg_edge, avg_perp, avg_spot in by_source:
        pct = count / total * 100 if total > 0 else 0
        emoji = "🔴" if source == "PERP" else "🔵" if source == "SPOT" else "🟣"
        msg += f"""
{emoji} <b>{source}:</b> {count} ({pct:.1f}%)
  Avg edge: {avg_edge:.2f} bps
  Avg PERP movement: {avg_perp:.2f} bps
  Avg SPOT movement: {avg_spot:.2f} bps
"""

    await update.message.reply_text(msg, parse_mode='HTML')


async def cmd_test_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /test_latest - Show last 5 opportunities
    """
    with pg_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                detected_at,
                edge_bps,
                volatility_source,
                perp_movement_bps,
                spot_movement_bps,
                expected_profit_hybrid - expected_profit_ioc as hybrid_advantage
            FROM opportunities
            ORDER BY detected_at DESC
            LIMIT 5
        """)
        opps = cur.fetchall()

    if not opps:
        await update.message.reply_text("❌ No opportunities tracked yet")
        return

    msg = "<b>📋 LATEST 5 OPPORTUNITIES:</b>\n\n"

    for i, (ts, edge, source, perp_mov, spot_mov, hybrid_adv) in enumerate(opps, 1):
        time_str = ts.strftime('%H:%M:%S')
        emoji = "🔴" if source == "PERP" else "🔵" if source == "SPOT" else "🟣"

        msg += f"""
<b>#{i}</b> - {time_str}
{emoji} Source: {source}
  Edge: {edge:.2f} bps
  PERP movement: {perp_mov:.2f} bps
  SPOT movement: {spot_mov:.2f} bps
  Hybrid advantage: {hybrid_adv:+.2f} bps
"""

    await update.message.reply_text(msg, parse_mode='HTML')


async def cmd_test_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /test_summary - Comprehensive analysis summary
    """
    with pg_conn() as conn:
        cur = conn.cursor()

        # Overall stats
        cur.execute("""
            SELECT
                COUNT(*) as total,
                AVG(edge_bps) as avg_edge,
                MIN(detected_at) as first_opp,
                MAX(detected_at) as last_opp,
                SUM(CASE WHEN volatility_source = 'PERP' THEN 1 ELSE 0 END) as perp_count,
                SUM(CASE WHEN volatility_source = 'SPOT' THEN 1 ELSE 0 END) as spot_count,
                AVG(CASE WHEN volatility_source = 'PERP' THEN perp_movement_bps END) as avg_perp_when_perp,
                AVG(CASE WHEN volatility_source = 'PERP' THEN spot_movement_bps END) as avg_spot_when_perp,
                AVG(CASE WHEN volatility_source = 'SPOT' THEN perp_movement_bps END) as avg_perp_when_spot,
                AVG(CASE WHEN volatility_source = 'SPOT' THEN spot_movement_bps END) as avg_spot_when_spot,
                AVG(expected_profit_hybrid - expected_profit_ioc) as avg_hybrid_advantage
            FROM opportunities
        """)
        stats = cur.fetchone()

    if not stats[0]:  # No data
        await update.message.reply_text("❌ No data collected yet")
        return

    total, avg_edge, first, last, perp_cnt, spot_cnt, \
        avg_perp_perp, avg_spot_perp, avg_perp_spot, avg_spot_spot, hybrid_adv = stats

    duration_hours = (last - first).total_seconds() / 3600
    perp_pct = perp_cnt / total * 100
    spot_pct = spot_cnt / total * 100

    msg = f"""
📊 <b>COMPREHENSIVE TEST SUMMARY</b>

⏰ <b>Collection Period:</b>
  Duration: {duration_hours:.1f} hours
  First: {first.strftime('%Y-%m-%d %H:%M')}
  Last: {last.strftime('%Y-%m-%d %H:%M')}

🎯 <b>Opportunities Detected:</b>
  Total: {total}
  Avg edge: {avg_edge:.2f} bps
  Rate: {total/duration_hours:.1f}/hour

📈 <b>Volatility Analysis:</b>

🔴 PERP-driven: {perp_cnt} ({perp_pct:.1f}%)
  When PERP volatile:
    • PERP movement: {avg_perp_perp:.2f} bps ← VOLATILE
    • SPOT movement: {avg_spot_perp:.2f} bps ← STABLE
    • Ratio: {avg_perp_perp/avg_spot_perp:.1f}x

🔵 SPOT-driven: {spot_cnt} ({spot_pct:.1f}%)
  When SPOT volatile:
    • PERP movement: {avg_perp_spot:.2f} bps ← STABLE
    • SPOT movement: {avg_spot_spot:.2f} bps ← VOLATILE
    • Ratio: {avg_spot_spot/avg_perp_spot:.1f}x

💰 <b>Strategy Comparison:</b>
  Avg hybrid advantage: {hybrid_adv:+.2f} bps/trade

📋 <b>Recommendation:</b>
"""

    if perp_pct >= 70:
        msg += "  ✅ STRONG SIGNAL for hybrid strategy!\n"
        msg += "  → Use IOC perp + ALO spot\n"
        msg += f"  → Expected gain: {hybrid_adv:.2f} bps/trade\n"
    elif spot_pct >= 70:
        msg += "  ⚠️  SPOT is volatile\n"
        msg += "  → Keep current strategy (IOC both)\n"
    else:
        msg += f"  ⚠️  Mixed results ({perp_pct:.0f}% vs {spot_pct:.0f}%)\n"
        msg += f"  → Need more data (target: 500+ opportunities)\n"

    await update.message.reply_text(msg, parse_mode='HTML')


# Register handlers
application.add_handler(CommandHandler("test_stats", cmd_test_stats))
application.add_handler(CommandHandler("test_latest", cmd_test_latest))
application.add_handler(CommandHandler("test_summary", cmd_test_summary))
```

---

## 🗄️ DATABASE MIGRATION

**File:** `migrations/add_opportunity_tracking.sql`

```sql
-- Create opportunities table for tracking
CREATE TABLE IF NOT EXISTS opportunities (
    id BIGSERIAL PRIMARY KEY,

    -- Timing
    detected_at TIMESTAMP WITH TIME ZONE NOT NULL,
    detection_latency_ms INTEGER,

    -- Market data
    edge_bps DOUBLE PRECISION NOT NULL,
    perp_bid DOUBLE PRECISION NOT NULL,
    perp_ask DOUBLE PRECISION NOT NULL,
    spot_bid DOUBLE PRECISION NOT NULL,
    spot_ask DOUBLE PRECISION NOT NULL,
    mid_ref DOUBLE PRECISION NOT NULL,

    -- Baseline
    baseline_perp_bid DOUBLE PRECISION,
    baseline_perp_ask DOUBLE PRECISION,
    baseline_spot_bid DOUBLE PRECISION,
    baseline_spot_ask DOUBLE PRECISION,

    -- Volatility analysis
    perp_bid_deviation_bps DOUBLE PRECISION,
    perp_ask_deviation_bps DOUBLE PRECISION,
    spot_bid_deviation_bps DOUBLE PRECISION,
    spot_ask_deviation_bps DOUBLE PRECISION,
    perp_movement_bps DOUBLE PRECISION,
    spot_movement_bps DOUBLE PRECISION,

    -- Classification
    volatility_source TEXT NOT NULL CHECK (volatility_source IN ('PERP', 'SPOT', 'BOTH')),
    volatility_ratio DOUBLE PRECISION,

    -- Strategy simulation
    would_trade_current BOOLEAN NOT NULL,
    cost_ioc_both DOUBLE PRECISION,
    cost_ioc_perp_alo_spot DOUBLE PRECISION,
    cost_alo_both DOUBLE PRECISION,
    expected_profit_ioc DOUBLE PRECISION,
    expected_profit_hybrid DOUBLE PRECISION,

    -- Metadata
    analysis_duration_ms INTEGER
);

-- Indexes for fast queries
CREATE INDEX idx_opportunities_detected_at ON opportunities(detected_at);
CREATE INDEX idx_opportunities_volatility_source ON opportunities(volatility_source);
CREATE INDEX idx_opportunities_edge_bps ON opportunities(edge_bps);
CREATE INDEX idx_opportunities_would_trade ON opportunities(would_trade_current);

-- Summary view for quick stats
CREATE OR REPLACE VIEW opportunity_summary AS
SELECT
    volatility_source,
    COUNT(*) as count,
    AVG(edge_bps) as avg_edge,
    AVG(perp_movement_bps) as avg_perp_movement,
    AVG(spot_movement_bps) as avg_spot_movement,
    AVG(volatility_ratio) as avg_ratio,
    AVG(expected_profit_hybrid - expected_profit_ioc) as avg_hybrid_advantage
FROM opportunities
GROUP BY volatility_source;
```

---

## ✅ TESTING CHECKLIST

### Phase 1: Development
- [ ] Create `opportunity_tracker.py` with all classes
- [ ] Add DB migration
- [ ] Integrate into `strategy.py` (non-breaking)
- [ ] Add Telegram commands
- [ ] Unit tests for tracker logic

### Phase 2: Testing (Dry Run)
- [ ] Start bot in test mode
- [ ] Manually trigger some edges
- [ ] Verify DB inserts
- [ ] Test Telegram commands
- [ ] Confirm main bot unaffected

### Phase 3: Deployment
- [ ] Run DB migration on production
- [ ] Deploy code (with tracker enabled)
- [ ] Monitor for 24 hours
- [ ] Check: Main bot working normally?
- [ ] Check: Tracker collecting data?

### Phase 4: Data Collection
- [ ] Let run for 1 week
- [ ] Collect 500+ opportunities
- [ ] Use `/test_summary` to analyze
- [ ] Make decision: implement hybrid strategy?

---

## 📏 SUCCESS CRITERIA

After 1 week of data collection:

✅ **Implement Hybrid Strategy IF:**
- PERP-driven ≥ 70%
- Average hybrid advantage ≥ +2 bps/trade
- No major issues with tracker
- Main bot performed normally

❌ **Keep Current Strategy IF:**
- SPOT-driven ≥ 60%
- Mixed results (no clear winner)
- Hybrid advantage < +1 bps/trade

🧪 **Collect More Data IF:**
- < 500 opportunities collected
- Unclear pattern
- Need more confidence

---

## 🚨 SAFETY MEASURES

1. **Isolation:** Tracker NEVER executes trades
2. **Error handling:** Tracker errors can't crash main bot
3. **Async writes:** DB writes don't block trading
4. **Monitoring:** Telegram alerts if tracker fails
5. **Rollback plan:** Can disable tracker instantly

---

## 📊 EXPECTED TIMELINE

| Phase | Duration | Deliverable |
|-------|----------|-------------|
| Development | 3-4 hours | Code complete |
| Testing | 1-2 hours | Verified working |
| Deployment | 30 mins | Live on prod |
| Data collection | 7 days | 500+ opportunities |
| Analysis | 1 hour | Decision made |
| **TOTAL** | **~8 days** | **Go/no-go decision** |

---

## 🎯 NEXT STEPS

Ready to implement? Here's the order:

1. ✅ Create DB migration
2. ✅ Implement `OpportunityTracker` class
3. ✅ Add storage functions
4. ✅ Integrate into `strategy.py`
5. ✅ Add Telegram commands
6. ✅ Test locally
7. ✅ Deploy to production
8. ✅ Monitor and collect data

**Shall we start?** 🚀
