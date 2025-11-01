"""
A/B Testing Framework

Allows testing different trading strategies (IOC on/off, different thresholds)
for fixed time periods and comparing their performance.

Usage:
    python -m bot.ab_tester
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import List, Dict, Any
import json

from .config import settings
from .runtime_config import get_runtime_config, get_trading_state
from .telegram_bot import get_telegram_notifier
from .storage import pg_conn


class TestScenario:
    """Defines a test scenario with specific parameters."""

    def __init__(self, name: str, threshold_bps: float, use_ioc: bool, description: str = ""):
        self.name = name
        self.threshold_bps = threshold_bps
        self.use_ioc = use_ioc
        self.spike_extra_bps = 0 if use_ioc else 7  # IOC=0, ALO=7
        self.description = description or f"Threshold: {threshold_bps} bps, IOC: {use_ioc}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "threshold_bps": self.threshold_bps,
            "use_ioc": self.use_ioc,
            "spike_extra_bps": self.spike_extra_bps,
            "description": self.description,
        }


class ABTester:
    """
    A/B Testing Framework for comparing trading strategies.
    """

    def __init__(self, test_duration_minutes: int = 30):
        """
        Args:
            test_duration_minutes: How long to run each test (default: 30 minutes)
        """
        self.test_duration = test_duration_minutes * 60  # Convert to seconds
        self.results: List[Dict[str, Any]] = []

    async def run_test(self, scenario: TestScenario) -> Dict[str, Any]:
        """
        Run a single test scenario.

        Returns:
            Dictionary with test results including PNL, trade count, etc.
        """
        print(f"\n{'='*60}")
        print(f"🧪 STARTING TEST: {scenario.name}")
        print(f"{'='*60}")
        print(f"Description: {scenario.description}")
        print(f"Duration: {self.test_duration / 60:.0f} minutes")
        print(f"Parameters:")
        print(f"  - Threshold: {scenario.threshold_bps} bps")
        print(f"  - IOC Mode: {'ON' if scenario.use_ioc else 'OFF'}")
        print(f"  - Spike Extra: {scenario.spike_extra_bps} bps")
        print(f"{'='*60}\n")

        # Get runtime config
        runtime_config = get_runtime_config()
        trading_state = get_trading_state()

        if not runtime_config or not trading_state:
            raise RuntimeError("Runtime config or trading state not initialized")

        # Notify via Telegram
        telegram = get_telegram_notifier()
        if telegram:
            await telegram.send_message(
                f"🧪 <b>A/B Test Starting</b>\n\n"
                f"<b>Scenario:</b> {scenario.name}\n"
                f"<b>Duration:</b> {self.test_duration / 60:.0f} minutes\n\n"
                f"<b>Parameters:</b>\n"
                f"• Threshold: {scenario.threshold_bps} bps\n"
                f"• IOC: {'ON' if scenario.use_ioc else 'OFF'}\n\n"
                f"Test will run until {datetime.now().strftime('%H:%M:%S')}"
            )

        # Apply scenario parameters
        runtime_config.set("threshold_bps", scenario.threshold_bps)
        runtime_config.set("spike_extra_bps_for_ioc", scenario.spike_extra_bps)

        # Ensure trading is enabled
        trading_state.start()

        # Record start state
        start_time = time.time()
        start_ts = datetime.now(timezone.utc)
        start_pnl = self._get_total_pnl()
        start_trades = self._get_trade_count()

        # Wait for test duration
        elapsed = 0
        while elapsed < self.test_duration:
            await asyncio.sleep(10)  # Check every 10 seconds
            elapsed = time.time() - start_time

            # Print progress every minute
            if int(elapsed) % 60 == 0 and elapsed > 0:
                remaining = (self.test_duration - elapsed) / 60
                print(f"⏱️ Test progress: {elapsed/60:.0f}/{self.test_duration/60:.0f} minutes ({remaining:.0f}m remaining)")

        # Record end state
        end_time = time.time()
        end_ts = datetime.now(timezone.utc)
        end_pnl = self._get_total_pnl()
        end_trades = self._get_trade_count()

        # Calculate results
        duration_minutes = (end_time - start_time) / 60
        pnl = end_pnl - start_pnl
        trade_count = end_trades - start_trades
        pnl_per_trade = pnl / trade_count if trade_count > 0 else 0
        pnl_per_hour = pnl / (duration_minutes / 60) if duration_minutes > 0 else 0

        # Get detailed trade breakdown
        successful_trades, failed_trades = self._get_trade_breakdown(start_ts, end_ts)

        result = {
            "scenario": scenario.to_dict(),
            "start_time": start_ts.isoformat(),
            "end_time": end_ts.isoformat(),
            "duration_minutes": duration_minutes,
            "pnl": pnl,
            "trade_count": trade_count,
            "successful_trades": successful_trades,
            "failed_trades": failed_trades,
            "success_rate": successful_trades / trade_count * 100 if trade_count > 0 else 0,
            "pnl_per_trade": pnl_per_trade,
            "pnl_per_hour": pnl_per_hour,
        }

        # Print results
        print(f"\n{'='*60}")
        print(f"✅ TEST COMPLETED: {scenario.name}")
        print(f"{'='*60}")
        print(f"Duration: {duration_minutes:.1f} minutes")
        print(f"PNL: ${pnl:.4f}")
        print(f"Trades: {trade_count} ({successful_trades} successful, {failed_trades} failed)")
        print(f"Success Rate: {result['success_rate']:.1f}%")
        print(f"PNL per Trade: ${pnl_per_trade:.4f}")
        print(f"PNL per Hour: ${pnl_per_hour:.4f}")
        print(f"{'='*60}\n")

        # Notify via Telegram
        if telegram:
            await telegram.send_message(
                f"✅ <b>Test Completed</b>\n\n"
                f"<b>Scenario:</b> {scenario.name}\n\n"
                f"<b>Results:</b>\n"
                f"• PNL: ${pnl:.4f}\n"
                f"• Trades: {trade_count} ({result['success_rate']:.1f}% success)\n"
                f"• PNL/Trade: ${pnl_per_trade:.4f}\n"
                f"• PNL/Hour: ${pnl_per_hour:.4f}"
            )

        return result

    async def run_multiple_tests(self, scenarios: List[TestScenario]) -> List[Dict[str, Any]]:
        """
        Run multiple test scenarios sequentially and compare results.

        Returns:
            List of results for each scenario
        """
        print(f"\n🚀 STARTING A/B TESTING")
        print(f"   Total scenarios: {len(scenarios)}")
        print(f"   Duration per test: {self.test_duration / 60:.0f} minutes")
        print(f"   Total time: {self.test_duration * len(scenarios) / 60:.0f} minutes\n")

        results = []

        for i, scenario in enumerate(scenarios, 1):
            print(f"\n📊 Running test {i}/{len(scenarios)}")
            result = await self.run_test(scenario)
            results.append(result)

            # Short break between tests (10 seconds)
            if i < len(scenarios):
                print(f"\n⏸️ Pausing for 10 seconds before next test...\n")
                await asyncio.sleep(10)

        # Find best scenario
        best_scenario = max(results, key=lambda x: x['pnl'])

        print(f"\n{'='*60}")
        print(f"🏆 A/B TESTING COMPLETE")
        print(f"{'='*60}\n")

        print(f"📊 RESULTS SUMMARY:\n")
        for result in results:
            print(f"{result['scenario']['name']}:")
            print(f"  PNL: ${result['pnl']:.4f}")
            print(f"  Trades: {result['trade_count']} ({result['success_rate']:.1f}% success)")
            print(f"  PNL/Hour: ${result['pnl_per_hour']:.4f}\n")

        print(f"🏆 BEST SCENARIO: {best_scenario['scenario']['name']}")
        print(f"   PNL: ${best_scenario['pnl']:.4f}")
        print(f"   PNL/Hour: ${best_scenario['pnl_per_hour']:.4f}")
        print(f"{'='*60}\n")

        # Notify via Telegram
        telegram = get_telegram_notifier()
        if telegram:
            summary = "🏆 <b>A/B Testing Complete</b>\n\n"
            for result in results:
                summary += f"<b>{result['scenario']['name']}</b>\n"
                summary += f"PNL: ${result['pnl']:.4f} ({result['trade_count']} trades)\n\n"

            summary += f"🥇 <b>Winner:</b> {best_scenario['scenario']['name']}\n"
            summary += f"PNL: ${best_scenario['pnl']:.4f}"

            await telegram.send_message(summary)

        return results

    def _get_total_pnl(self) -> float:
        """Get total realized PNL from closed positions."""
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE status = 'CLOSED'"
                )
                return float(cur.fetchone()[0])
        except Exception as e:
            print(f"⚠️ Error fetching PNL: {e}")
            return 0.0

    def _get_trade_count(self) -> int:
        """Get total number of trades."""
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM trades")
                return int(cur.fetchone()[0])
        except Exception as e:
            print(f"⚠️ Error fetching trade count: {e}")
            return 0

    def _get_trade_breakdown(self, start_ts: datetime, end_ts: datetime) -> tuple[int, int]:
        """Get breakdown of successful vs failed trades in time window."""
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'POSTED') as successful,
                        COUNT(*) FILTER (WHERE status IN ('FAILED', 'ERROR')) as failed
                    FROM trades
                    WHERE ts >= %s AND ts <= %s
                    """,
                    (start_ts, end_ts)
                )
                row = cur.fetchone()
                return (row[0] or 0, row[1] or 0)
        except Exception as e:
            print(f"⚠️ Error fetching trade breakdown: {e}")
            return (0, 0)


# Predefined test scenarios
QUICK_TEST_SCENARIOS = [
    TestScenario("IOC_ON_20", threshold_bps=20, use_ioc=True, description="IOC ON, 20 bps threshold"),
    TestScenario("IOC_OFF_15", threshold_bps=15, use_ioc=False, description="IOC OFF, 15 bps threshold"),
    TestScenario("IOC_OFF_10", threshold_bps=10, use_ioc=False, description="IOC OFF, 10 bps threshold"),
]
