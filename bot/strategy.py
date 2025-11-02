import json
from datetime import datetime, timezone
from typing import Optional

from .config import settings
from .execution import HyperliquidTrader, WsPostSession
from .hl_client import compute_edges
from .notifier import send_trade_email
from .storage import insert_edge, insert_trade, insert_position, get_open_positions
from .storage_async import get_batch_writer
from .position_manager import PositionManager
from .telegram_bot import get_telegram_notifier
from .runtime_config import get_runtime_config, get_trading_state
from .opportunity_tracker import OpportunityTracker
class RateCap:
    def __init__(self, limit_per_min:int):
        self.limit = limit_per_min
        self.bucket = []
    def allow(self, now):
        self.bucket = [t for t in self.bucket if (now - t).total_seconds() < 60]
        if len(self.bucket) < self.limit:
            self.bucket.append(now)
            return True
        return False
class Strategy:
    def __init__(self, spot_index:int, broadcast, trader: Optional[HyperliquidTrader] = None, deadman_ms: int = 5000):
        self.spot_index = spot_index
        self.broadcast = broadcast
        self.rater = RateCap(settings.max_trades_per_min)
        self.trader = trader
        self.deadman_ms = deadman_ms
        self.position_manager = PositionManager(trader) if trader else None

        # 🧪 OPPORTUNITY TRACKER: Non-intrusive data collection for 10+ bps opportunities
        # Main bot trades at 20 bps (unchanged), tracker monitors all 10+ bps for analysis
        self.opportunity_tracker = OpportunityTracker(tracking_threshold_bps=10.0)

    def attach_post_session(self, session: Optional[WsPostSession]) -> None:
        if self.trader:
            self.trader.attach_session(session)

    async def check_capital_available(self, direction: str, alloc_usd: float) -> tuple[bool, Optional[str]]:
        """
        Check if we have sufficient capital/inventory to execute the trade.

        For perp->spot (PERP SHORT + SPOT BUY):
        - Need USDC in spot wallet to buy HYPE
        - Need margin in perp wallet to open SHORT

        For spot->perp (PERP LONG + SPOT SELL):
        - Need HYPE in spot wallet to sell
        - Need margin in perp wallet to open LONG
        """
        if not self.trader:
            return (True, None)

        try:
            # Get balances from Hyperliquid
            from .rebalancer import CapitalRebalancer
            import asyncio

            loop = asyncio.get_event_loop()
            rebalancer = await loop.run_in_executor(None, CapitalRebalancer)
            balances = await loop.run_in_executor(None, rebalancer.get_balances)

            perp_usdc = balances["perp_usdc"]
            spot_usdc = balances["spot_usdc"]
            spot_hype = balances["spot_hype"]
            hype_price = balances["hype_mid_price"]

            # Calculate required amounts (with safety buffer)
            # For $10 trade with 3x leverage: need ~$4 margin + $10 spot
            required_perp_margin = alloc_usd / settings.leverage * 1.2  # 20% buffer
            required_spot_usdc = alloc_usd * 1.05  # 5% buffer for slippage

            if direction == "perp->spot":
                # 🔵 perp->spot: PERP SHORT + SPOT BUY
                # Need: Perp margin for SHORT, Spot USDC to BUY HYPE

                if perp_usdc < required_perp_margin:
                    msg = f"Insufficient perp margin: ${perp_usdc:.2f} < ${required_perp_margin:.2f}"
                    print(f"⚠️ {msg}")
                    return (False, msg)

                if spot_usdc < required_spot_usdc:
                    msg = f"Insufficient spot USDC: ${spot_usdc:.2f} < ${required_spot_usdc:.2f}"
                    print(f"⚠️ {msg}")
                    return (False, msg)

            else:
                # 🔴 spot->perp: PERP LONG + SPOT SELL
                # Need: Perp margin for LONG, Spot HYPE to SELL

                if perp_usdc < required_perp_margin:
                    msg = f"Insufficient perp margin: ${perp_usdc:.2f} < ${required_perp_margin:.2f}"
                    print(f"⚠️ {msg}")
                    return (False, msg)

                # Calculate required HYPE amount
                if hype_price > 0:
                    required_hype = (alloc_usd / hype_price) * 1.05  # 5% buffer

                    if spot_hype < required_hype:
                        spot_hype_value = spot_hype * hype_price
                        msg = f"Insufficient spot HYPE: {spot_hype:.4f} (${spot_hype_value:.2f}) < {required_hype:.4f} (${required_spot_usdc:.2f})"
                        print(f"⚠️ {msg}")
                        return (False, msg)

            # All checks passed
            return (True, None)

        except Exception as e:
            print(f"⚠️ Capital check failed (allowing trade anyway): {e}")
            # If balance check fails, allow trade (fail open, not closed)
            return (True, None)
    async def on_edge(self, pbid, pask, sbid, sask, recv_ms: int):
        # Get runtime config and trading state
        runtime_config = get_runtime_config()
        trading_state = get_trading_state()

        # Get current settings (runtime overrides or defaults)
        threshold_bps = runtime_config.get("threshold_bps", settings.threshold_bps) if runtime_config else settings.threshold_bps
        spike_extra_bps = runtime_config.get("spike_extra_bps_for_ioc", settings.spike_extra_bps_for_ioc) if runtime_config else settings.spike_extra_bps_for_ioc
        dry_run = runtime_config.get("dry_run", settings.dry_run) if runtime_config else settings.dry_run
        alloc_usd = runtime_config.get("alloc_per_trade_usd", settings.alloc_per_trade_usd) if runtime_config else settings.alloc_per_trade_usd

        edges = compute_edges(pbid,pask,sbid,sask,{
            "perp":{"maker":settings.perp_maker_bps,"taker":settings.perp_taker_bps},
            "spot":{"maker":settings.spot_maker_bps,"taker":settings.spot_taker_bps},
        })

        # Update trading state with latest edges
        if trading_state:
            trading_state.update_edges(edges["ps_mm"], edges["sp_mm"], edges["mid_ref"])

        if self.trader:
            self.trader.update_mid_prices(pbid, pask, sbid, sask)

        # Position monitoring - açık pozisyonları kontrol et ve gerekirse kapat
        if self.position_manager and not dry_run:
            await self.position_manager.monitor_positions(pbid, pask, sbid, sask)

        # 🎯 SINGLE DIRECTION OPTIMIZATION: Only perp→spot (93% of trades, profitable)
        # spot→perp disabled (7% of trades, unprofitable)
        mm_best = edges["ps_mm"]

        # 🧪 OPPORTUNITY TRACKER: Record all 10+ bps opportunities for analysis
        # This runs on EVERY tick but only records when edge >= 10 bps
        # Wrapped in try/except to ensure tracker errors never crash main bot
        try:
            await self.opportunity_tracker.on_edge(pbid, pask, sbid, sask, mm_best)
        except Exception as tracker_error:
            # Log error but continue with main bot operation
            print(f"⚠️ OpportunityTracker error (non-critical): {tracker_error}")
        direction = "perp->spot"
        ts = datetime.now(timezone.utc)
        payload = {"ts": ts.isoformat(), "base": settings.pair_base, "spot_index": self.spot_index, "edge_ps_mm_bps": edges["ps_mm"], "edge_sp_mm_bps": edges["sp_mm"], "mid_ref": edges["mid_ref"], "latency_ms": recv_ms, "threshold_bps": threshold_bps}
        await self.broadcast(payload)

        # 🚀 PERFORMANCE: Async batch write (non-blocking, ~5-8ms saved)
        batch_writer = get_batch_writer()
        if batch_writer:
            await batch_writer.queue_edge(ts, settings.pair_base, self.spot_index, edges["ps_mm"], edges["sp_mm"], edges["mid_ref"], recv_ms, 0)
        else:
            # Fallback to sync insert if batch writer not initialized
            insert_edge(ts, settings.pair_base, self.spot_index, edges["ps_mm"], edges["sp_mm"], edges["mid_ref"], recv_ms, 0)

        # Check if trading is enabled
        if trading_state and not trading_state.is_running():
            # Trading is paused, don't execute trades
            return

        if mm_best >= threshold_bps:
            if not self.rater.allow(ts):
                return
            role = "maker_first"
            use_ioc = mm_best >= (threshold_bps + spike_extra_bps)
            status = "SIMULATED"
            req = {
                "direction": direction,
                "mm_best_bps": mm_best,
                "alloc_usd": alloc_usd,
                "role": role,
                "tif": "Ioc" if use_ioc else "Alo",
                "deadman_ms": 0 if use_ioc else self.deadman_ms,
            }
            resp = {"ok": True, "note": "DRY_RUN - no real order placed"}
            request_id = None

            if dry_run:
                pass
            elif not self.trader:
                status = "SKIPPED"
                resp = {"ok": False, "error": "Trader not configured"}
            elif not self.trader.ready:
                status = "DELAYED"
                resp = {"ok": False, "error": "Trader session unavailable"}
            else:
                # 🛡️ MAX POSITIONS CHECK - Prevent overexposure
                open_positions = get_open_positions()
                if len(open_positions) >= 2:
                    print(f"⚠️ MAX POSITIONS REACHED: {len(open_positions)}/2 open positions")
                    status = "SKIPPED"
                    resp = {"ok": False, "error": "Max positions (2) reached"}
                    # Don't record this as a failed trade
                    return

                # 💰 CAPITAL/INVENTORY CHECK - Prevent invalid orders
                capital_ok, capital_error = await self.check_capital_available(direction, alloc_usd)
                if not capital_ok:
                    print(f"⚠️ CAPITAL CHECK FAILED: {capital_error}")
                    status = "SKIPPED"
                    resp = {"ok": False, "error": capital_error}
                    # Don't record this as a failed trade
                    return

                # Execute trade
                try:
                    exec_result = await self.trader.execute(
                        direction,
                        mm_best,
                        use_ioc,
                        pbid,
                        pask,
                        sbid,
                        sask,
                        self.deadman_ms,
                    )
                    req.update(exec_result.get("request", {}))
                    response_payload = exec_result.get("response") or {}
                    response_payload["ok"] = exec_result.get("ok", False)
                    resp = response_payload
                    status = "POSTED" if exec_result.get("ok") else "FAILED"
                    request_id = exec_result.get("request_id")
                except Exception as exc:
                    status = "ERROR"
                    resp = {"ok": False, "error": repr(exc)}

            trade_id = insert_trade(
                ts,
                settings.pair_base,
                direction,
                settings.threshold_bps,
                mm_best,
                settings.alloc_per_trade_usd,
                role,
                request_id,
                json.dumps(req),
                json.dumps(resp),
                status,
            )

            # Log failed trades (auto-rebalancer removed)
            if status in ("FAILED", "ERROR"):
                print(f"\n❌ TRADE {status}")
                print(f"   Direction: {direction}")
                print(f"   Edge: {mm_best:.2f} bps")
                print(f"   Response: {json.dumps(resp, indent=2)}")

                # Notify via Telegram
                telegram = get_telegram_notifier()
                if telegram:
                    await telegram.notify_error(
                        "Trade Failed",
                        f"Direction: {direction}\nEdge: {mm_best:.2f} bps\nError: {resp.get('error', 'Unknown')}"
                    )

            # Successful trade - track position
            if status == "POSTED":

                # Notify successful trade via Telegram
                telegram = get_telegram_notifier()
                if telegram:
                    details = f"TIF: {req.get('tif', 'N/A')}"
                    await telegram.notify_trade(direction, mm_best, status, alloc_usd, details)

            if status == "POSTED" and not dry_run:
                try:
                    # Order detaylarını parse et
                    orders = req.get("orders", [])
                    perp_order = None
                    spot_order = None

                    for order in orders:
                        if order.get("coin") == settings.pair_base:
                            perp_order = order
                        else:
                            spot_order = order

                    if perp_order and spot_order:
                        insert_position(
                            opened_at=ts,
                            base=settings.pair_base,
                            direction=direction,
                            open_edge_bps=mm_best,
                            perp_size=abs(perp_order.get("sz", 0)),
                            spot_size=abs(spot_order.get("sz", 0)),
                            perp_entry_px=perp_order.get("limit_px", 0),
                            spot_entry_px=spot_order.get("limit_px", 0),
                            timeout_seconds=300,  # 5 dakika
                            trade_id=trade_id
                        )
                        print(f"📍 Position tracked: {direction}, edge: {mm_best:.2f} bps")
                except Exception as e:
                    print(f"⚠️  Failed to track position: {e}")

            subject = f"[HL-ARB] {settings.pair_base}/USDC edge {mm_best:.2f} bps >= {settings.threshold_bps}"
            body = f"Edge crossed threshold:\n\nPair: {settings.pair_base}/USDC\nDirection: {direction}\nEdge (mm_best): {mm_best:.4f} bps\nThreshold: {settings.threshold_bps} bps\nAlloc per trade: ${settings.alloc_per_trade_usd}\nRole: {role}\nStatus: {status}\nRequest: {json.dumps(req)}\nResponse: {json.dumps(resp)}\nTimestamp: {ts.isoformat()}\n"
            send_trade_email(subject, body)
