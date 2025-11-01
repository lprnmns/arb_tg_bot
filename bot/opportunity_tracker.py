"""
Opportunity Tracker for Volatility Analysis and Strategy Testing

This module tracks all arbitrage opportunities above 10 bps to collect data for
analyzing volatility sources (PERP vs SPOT) and testing adaptive trading strategies.

Key features:
- Non-intrusive: Errors do not affect main bot trading
- Rolling baseline tracking (20-tick window)
- Volatility source classification (PERP/SPOT/BOTH)
- Strategy cost simulation and profit projections
- Async database storage for non-blocking operation
"""

import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import asyncio

from .config import settings
from .storage_async import get_batch_writer


class RollingBaseline:
    """
    Tracks rolling average of market prices over last N ticks.
    Used to measure deviations from "normal" state.
    """
    def __init__(self, window_size: int = 20):
        self.window_size = window_size
        self.perp_bids = deque(maxlen=window_size)
        self.perp_asks = deque(maxlen=window_size)
        self.spot_bids = deque(maxlen=window_size)
        self.spot_asks = deque(maxlen=window_size)

    def update(self, perp_bid: float, perp_ask: float, spot_bid: float, spot_ask: float):
        """Add new tick to baseline tracking"""
        self.perp_bids.append(perp_bid)
        self.perp_asks.append(perp_ask)
        self.spot_bids.append(spot_bid)
        self.spot_asks.append(spot_ask)

    def is_ready(self) -> bool:
        """Check if we have enough data for baseline"""
        return len(self.perp_bids) >= self.window_size

    def get_baseline(self) -> Dict[str, float]:
        """Get current baseline averages"""
        if not self.is_ready():
            return {
                "perp_bid": 0.0,
                "perp_ask": 0.0,
                "spot_bid": 0.0,
                "spot_ask": 0.0,
            }

        return {
            "perp_bid": sum(self.perp_bids) / len(self.perp_bids),
            "perp_ask": sum(self.perp_asks) / len(self.perp_asks),
            "spot_bid": sum(self.spot_bids) / len(self.spot_bids),
            "spot_ask": sum(self.spot_asks) / len(self.spot_asks),
        }


class OpportunityTracker:
    """
    Tracks arbitrage opportunities for volatility analysis and strategy testing.

    This tracker:
    1. Monitors all edges (updated on every tick)
    2. Records opportunities when edge >= 10 bps
    3. Analyzes which side (PERP/SPOT) is volatile
    4. Simulates costs for different strategies
    5. Stores detailed data for later analysis
    """

    def __init__(self, tracking_threshold_bps: float = 10.0):
        """
        Initialize opportunity tracker.

        Args:
            tracking_threshold_bps: Minimum edge (in bps) to track as opportunity
        """
        self.tracking_threshold = tracking_threshold_bps
        self.baseline = RollingBaseline(window_size=20)
        self.opportunities_tracked = 0
        self.last_opportunity_time: Optional[datetime] = None

        # Fee structure (same as main bot)
        self.perp_maker_bps = settings.perp_maker_bps
        self.perp_taker_bps = settings.perp_taker_bps
        self.spot_maker_bps = settings.spot_maker_bps
        self.spot_taker_bps = settings.spot_taker_bps

        print(f"✅ OpportunityTracker initialized: tracking_threshold={tracking_threshold_bps} bps")

    def get_stats(self) -> Dict[str, Any]:
        """Get current tracking statistics"""
        return {
            "opportunities_tracked": self.opportunities_tracked,
            "last_opportunity_time": self.last_opportunity_time.isoformat() if self.last_opportunity_time else None,
            "baseline_ready": self.baseline.is_ready(),
            "tracking_threshold_bps": self.tracking_threshold,
        }

    async def on_edge(self, perp_bid: float, perp_ask: float, spot_bid: float, spot_ask: float, edge_bps: float):
        """
        Called on every edge update from strategy.

        This method:
        1. Always updates the rolling baseline
        2. If edge >= threshold, records full opportunity analysis
        3. Never raises exceptions (wrapped in try/except in strategy)

        Args:
            perp_bid, perp_ask: Perpetual market prices
            spot_bid, spot_ask: Spot market prices
            edge_bps: Calculated edge in basis points
        """
        start_time = time.perf_counter()

        # Always update baseline (needed for deviation calculations)
        self.baseline.update(perp_bid, perp_ask, spot_bid, spot_ask)

        # Only track opportunities above threshold
        if edge_bps < self.tracking_threshold:
            return

        # Skip if baseline not ready yet (need 20 ticks)
        if not self.baseline.is_ready():
            return

        # Record opportunity
        detected_at = datetime.now(timezone.utc)
        baseline = self.baseline.get_baseline()

        # Calculate deviations from baseline (in bps)
        deviations = self._calculate_deviations(
            perp_bid, perp_ask, spot_bid, spot_ask, baseline
        )

        # Analyze volatility source
        volatility = self._analyze_volatility(deviations)

        # Simulate strategy costs
        costs = self._simulate_costs()

        # Calculate expected profits
        profits = {
            "ioc_both": edge_bps - costs["ioc_both"],
            "adaptive": edge_bps - costs[volatility["best_strategy"]],
        }

        # Calculate analysis duration
        analysis_duration_ms = int((time.perf_counter() - start_time) * 1000)

        # Prepare opportunity record
        opportunity = {
            "detected_at": detected_at,
            "detection_latency_ms": None,  # TODO: Could measure from WS receive to here
            "edge_bps": edge_bps,
            "perp_bid": perp_bid,
            "perp_ask": perp_ask,
            "spot_bid": spot_bid,
            "spot_ask": spot_ask,
            "baseline_perp_bid": baseline["perp_bid"],
            "baseline_perp_ask": baseline["perp_ask"],
            "baseline_spot_bid": baseline["spot_bid"],
            "baseline_spot_ask": baseline["spot_ask"],
            "perp_bid_deviation_bps": deviations["perp_bid_bps"],
            "perp_ask_deviation_bps": deviations["perp_ask_bps"],
            "spot_bid_deviation_bps": deviations["spot_bid_bps"],
            "spot_ask_deviation_bps": deviations["spot_ask_bps"],
            "perp_movement_bps": deviations["perp_movement_bps"],
            "spot_movement_bps": deviations["spot_movement_bps"],
            "volatility_source": volatility["source"],
            "volatility_ratio": volatility["ratio"],
            "cost_ioc_both": costs["ioc_both"],
            "cost_ioc_perp_alo_spot": costs["ioc_perp_alo_spot"],
            "cost_ioc_spot_alo_perp": costs["ioc_spot_alo_perp"],
            "expected_profit_ioc_both": profits["ioc_both"],
            "expected_profit_adaptive": profits["adaptive"],
            "analysis_duration_ms": analysis_duration_ms,
        }

        # Store asynchronously (non-blocking)
        await self._store_opportunity(opportunity)

        # Update stats
        self.opportunities_tracked += 1
        self.last_opportunity_time = detected_at

    def _calculate_deviations(
        self,
        perp_bid: float,
        perp_ask: float,
        spot_bid: float,
        spot_ask: float,
        baseline: Dict[str, float],
    ) -> Dict[str, float]:
        """
        Calculate deviations from baseline in basis points.

        For perp->spot arbitrage, we care about:
        - perp_ask deviation (we buy perp at ask)
        - spot_bid deviation (we sell spot at bid)
        """
        mid_ref = (perp_bid + perp_ask + spot_bid + spot_ask) / 4.0

        # Deviation = (current - baseline) / mid * 10000 bps
        perp_bid_dev_bps = ((perp_bid - baseline["perp_bid"]) / mid_ref) * 10000
        perp_ask_dev_bps = ((perp_ask - baseline["perp_ask"]) / mid_ref) * 10000
        spot_bid_dev_bps = ((spot_bid - baseline["spot_bid"]) / mid_ref) * 10000
        spot_ask_dev_bps = ((spot_ask - baseline["spot_ask"]) / mid_ref) * 10000

        # Movement = absolute deviation (how much each side moved)
        perp_movement_bps = abs(perp_ask_dev_bps)
        spot_movement_bps = abs(spot_bid_dev_bps)

        return {
            "perp_bid_bps": perp_bid_dev_bps,
            "perp_ask_bps": perp_ask_dev_bps,
            "spot_bid_bps": spot_bid_dev_bps,
            "spot_ask_bps": spot_ask_dev_bps,
            "perp_movement_bps": perp_movement_bps,
            "spot_movement_bps": spot_movement_bps,
        }

    def _analyze_volatility(self, deviations: Dict[str, float]) -> Dict[str, Any]:
        """
        Classify volatility source based on which side moved more.

        Returns:
            - source: 'PERP', 'SPOT', or 'BOTH'
            - ratio: primary_movement / secondary_movement
            - best_strategy: Which cost model to use
        """
        perp_mov = deviations["perp_movement_bps"]
        spot_mov = deviations["spot_movement_bps"]

        # Avoid division by zero
        if spot_mov < 0.01:
            source = "PERP"
            ratio = 999.9  # Effectively infinite
            best_strategy = "ioc_perp_alo_spot"
        elif perp_mov < 0.01:
            source = "SPOT"
            ratio = 999.9
            best_strategy = "ioc_spot_alo_perp"
        elif perp_mov > spot_mov * 1.5:
            # PERP moved significantly more than SPOT
            source = "PERP"
            ratio = perp_mov / spot_mov
            best_strategy = "ioc_perp_alo_spot"
        elif spot_mov > perp_mov * 1.5:
            # SPOT moved significantly more than PERP
            source = "SPOT"
            ratio = spot_mov / perp_mov
            best_strategy = "ioc_spot_alo_perp"
        else:
            # Both moved similarly
            source = "BOTH"
            ratio = max(perp_mov, spot_mov) / max(min(perp_mov, spot_mov), 0.01)
            best_strategy = "ioc_both"

        return {
            "source": source,
            "ratio": ratio,
            "best_strategy": best_strategy,
        }

    def _simulate_costs(self) -> Dict[str, float]:
        """
        Simulate costs for different strategies.

        Strategies:
        1. ioc_both: IOC open + IOC close (current strategy baseline)
        2. ioc_perp_alo_spot: IOC perp + ALO spot (if PERP-driven)
        3. ioc_spot_alo_perp: IOC spot + ALO perp (if SPOT-driven)

        Returns costs in basis points.
        """
        # Strategy 1: IOC both sides (taker fees both ways)
        # Open: perp taker + spot taker = 4.5 + 7.0 = 11.5 bps
        # Close: perp taker + spot taker = 4.5 + 7.0 = 11.5 bps
        # But we use hybrid: IOC open (11.5) + ALO close with 80% success
        # 80% ALO (5.5 bps) + 20% IOC fallback (11.5 bps) = 6.7 bps avg close
        cost_ioc_both = 11.5 + 6.7  # 18.2 bps

        # Strategy 2: IOC perp + ALO spot (if PERP volatile, SPOT stable)
        # Open: perp taker (4.5) + spot maker (4.0) = 8.5 bps
        # Close: perp maker (1.5) + spot maker (4.0) = 5.5 bps (ALO both sides)
        cost_ioc_perp_alo_spot = 8.5 + 5.5  # 14.0 bps

        # Strategy 3: IOC spot + ALO perp (if SPOT volatile, PERP stable)
        # Open: perp maker (1.5) + spot taker (7.0) = 8.5 bps
        # Close: perp maker (1.5) + spot maker (4.0) = 5.5 bps (ALO both sides)
        cost_ioc_spot_alo_perp = 8.5 + 5.5  # 14.0 bps

        return {
            "ioc_both": cost_ioc_both,
            "ioc_perp_alo_spot": cost_ioc_perp_alo_spot,
            "ioc_spot_alo_perp": cost_ioc_spot_alo_perp,
        }

    async def _store_opportunity(self, opportunity: Dict[str, Any]):
        """
        Store opportunity record to database asynchronously.

        Uses batch writer if available for non-blocking operation.
        """
        try:
            batch_writer = get_batch_writer()
            if batch_writer:
                # Queue for async batch write (non-blocking)
                await batch_writer.queue_opportunity(opportunity)
            else:
                # Fallback: direct insert (blocking, but safer)
                # Note: We'd need to add direct insert function to storage.py
                # For now, just log that batch writer is not available
                print(f"⚠️ OpportunityTracker: batch_writer not available, skipping storage")
        except Exception as e:
            # Never let storage errors crash the tracker
            print(f"❌ OpportunityTracker storage error: {e}")
