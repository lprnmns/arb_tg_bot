"""
Telegram bot for HL Arbitrage monitoring and control.

Features:
- Real-time trade notifications
- PNL tracking and reporting
- Balance checks
- Position monitoring
- Manual rebalancing
- Trade history with filters
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes, Updater

from .config import settings
from .storage import pg_conn
from .rebalancer import rebalance_capital_sync, CapitalRebalancer
from .runtime_config import get_runtime_config, get_trading_state


class TelegramNotifier:
    """
    Telegram bot for notifications and commands.
    """

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.app: Optional[Application] = None
        self.updater: Optional[Updater] = None

    async def start_bot(self):
        """Initialize and start the bot."""
        # Create bot instance
        bot = Bot(token=self.token)

        # Create updater with bot
        self.updater = Updater(bot=bot, update_queue=asyncio.Queue())

        # Build application with the updater (no token needed)
        self.app = (
            Application.builder()
            .updater(self.updater)
            .build()
        )

        # Register command handlers
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("balance", self.cmd_balance))
        self.app.add_handler(CommandHandler("trades", self.cmd_trades))
        self.app.add_handler(CommandHandler("positions", self.cmd_positions))
        self.app.add_handler(CommandHandler("pnl", self.cmd_pnl))
        self.app.add_handler(CommandHandler("stats", self.cmd_stats))
        self.app.add_handler(CommandHandler("rebalance", self.cmd_rebalance))

        # Control commands
        self.app.add_handler(CommandHandler("stop_trade", self.cmd_stop_bot))
        self.app.add_handler(CommandHandler("start_trade", self.cmd_start_bot))
        self.app.add_handler(CommandHandler("edges", self.cmd_edges))
        self.app.add_handler(CommandHandler("config", self.cmd_config))
        self.app.add_handler(CommandHandler("set", self.cmd_set))
        self.app.add_handler(CommandHandler("test", self.cmd_test))  # A/B testing

        # Opportunity tracking commands
        self.app.add_handler(CommandHandler("test_stats", self.cmd_test_stats))
        self.app.add_handler(CommandHandler("test_latest", self.cmd_test_latest))
        self.app.add_handler(CommandHandler("test_summary", self.cmd_test_summary))

        # Initialize the application
        await self.app.initialize()
        await self.app.start()

        print(f"✅ Telegram bot initialized (chat_id: {self.chat_id})")
        print(f"🔄 Starting polling...")

        # Start polling in background (non-blocking)
        asyncio.create_task(self._run_polling())

    async def _run_polling(self):
        """Run polling in background."""
        try:
            # Start the updater and polling
            await self.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
            print(f"✅ Telegram polling started successfully")
        except Exception as e:
            print(f"❌ Telegram polling error: {e}")
            import traceback
            traceback.print_exc()

    async def stop_bot(self):
        """Stop the bot gracefully."""
        if self.updater:
            await self.updater.stop()
        if self.app:
            await self.app.stop()
            await self.app.shutdown()

    async def send_message(self, text: str, parse_mode: str = "HTML"):
        """Send a message to the configured chat."""
        if not self.app:
            print(f"⚠️  Telegram bot not initialized, skipping message: {text[:50]}")
            return

        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode
            )
        except Exception as e:
            print(f"❌ Failed to send Telegram message: {e}")

    # ===== COMMAND HANDLERS =====

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        welcome_text = (
            "🤖 <b>HL Arbitrage Bot</b>\n\n"
            "Welcome! I monitor HYPE/USDC spot-perp arbitrage.\n\n"
            "Use /help to see available commands."
        )
        await update.message.reply_text(welcome_text, parse_mode="HTML")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command."""
        help_text = (
            "📚 <b>Available Commands</b>\n\n"
            "<b>Monitoring:</b>\n"
            "/status - Bot status and uptime\n"
            "/balance - Current capital balances\n"
            "/positions - Open positions\n"
            "/edges - Live edge values (both directions)\n\n"
            "<b>History:</b>\n"
            "/trades [hours] - Recent trades (default: 1h)\n"
            "/pnl [hours] - PNL summary (default: 24h)\n"
            "/stats - Overall statistics\n\n"
            "<b>Control:</b>\n"
            "/stop_trade - Stop trading (pause)\n"
            "/start_trade - Resume trading\n"
            "/rebalance - Check and rebalance capital\n"
            "/test - Run A/B tests (3x30min scenarios)\n\n"
            "<b>Opportunity Tracking:</b>\n"
            "/test_stats - Tracker statistics\n"
            "/test_latest - Last 5 opportunities\n"
            "/test_summary - Full analysis summary\n\n"
            "<b>Settings:</b>\n"
            "/config - Show current settings\n"
            "/set threshold &lt;value&gt; - Set threshold BPS\n"
            "/set dryrun &lt;on/off&gt; - Toggle dry run mode\n"
            "/set ioc &lt;on/off&gt; - Toggle IOC mode\n"
            "/set alloc &lt;value&gt; - Set trade size (USD)\n\n"
            "<i>Examples:</i>\n"
            "• /trades 6 - Last 6 hours\n"
            "• /set threshold 15 - Set 15 bps threshold\n"
            "• /test - Run strategy tests\n"
            "• /test_stats - Check data collection"
        )
        await update.message.reply_text(help_text, parse_mode="HTML")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command."""
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                # Get latest edge timestamp
                cur.execute("SELECT ts FROM edges ORDER BY ts DESC LIMIT 1")
                result = cur.fetchone()
                last_edge = result[0] if result else None

                # Get today's trade count
                cur.execute(
                    "SELECT COUNT(*) FROM trades WHERE ts > NOW() - INTERVAL '24 hours'"
                )
                today_trades = cur.fetchone()[0]

                # Get open positions
                cur.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
                open_positions = cur.fetchone()[0]

            status_text = (
                f"📊 <b>Bot Status</b>\n\n"
                f"🟢 <b>Active</b>\n"
                f"⏰ Last Update: {last_edge.strftime('%H:%M:%S UTC') if last_edge else 'N/A'}\n"
                f"📈 Today's Trades: {today_trades}\n"
                f"📍 Open Positions: {open_positions}\n"
                f"🎯 Threshold: {settings.threshold_bps} bps\n"
                f"💰 Size: ${settings.alloc_per_trade_usd} per trade\n"
                f"🔧 Mode: {'DRY RUN' if settings.dry_run else 'LIVE'}"
            )
            await update.message.reply_text(status_text, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /balance command."""
        loading_msg = None
        try:
            # Send "loading" message
            loading_msg = await update.message.reply_text("⏳ Fetching balances...")

            # Run sync code in thread pool
            def get_balance_data():
                rebalancer = CapitalRebalancer()
                balances = rebalancer.get_balances()
                actions = rebalancer.calculate_rebalance_actions(balances, min_transfer_usd=5.0)
                return balances, actions

            balances, actions = await asyncio.to_thread(get_balance_data)

            total_usdc = balances['perp_usdc'] + balances['spot_usdc']
            spot_hype_value = balances['spot_hype'] * balances['hype_mid_price']
            total_value = total_usdc + spot_hype_value

            balanced_emoji = "✅" if not actions["needs_rebalance"] else "⚠️"

            balance_text = (
                f"💰 <b>Capital Balances</b> {balanced_emoji}\n\n"
                f"<b>Perp:</b>\n"
                f"  USDC: ${balances['perp_usdc']:.2f}\n\n"
                f"<b>Spot:</b>\n"
                f"  USDC: ${balances['spot_usdc']:.2f}\n"
                f"  HYPE: {balances['spot_hype']:.4f}\n"
                f"  HYPE Value: ${spot_hype_value:.2f}\n\n"
                f"<b>Total Portfolio:</b> ${total_value:.2f}\n"
                f"HYPE Price: ${balances['hype_mid_price']:.2f}\n\n"
            )

            if actions["needs_rebalance"]:
                balance_text += (
                    f"⚠️ <b>Rebalance Needed (50-50 target)</b>\n"
                    f"Target Perp: ${actions['target_perp_usdc']:.2f}\n"
                    f"Target Spot: ${actions['target_spot_usdc']:.2f}\n"
                    f"Use /rebalance to fix"
                )
            else:
                balance_text += f"✅ Balanced (50-50 split: ${actions['target_perp_usdc']:.2f} each)"

            # Delete loading message and send result
            await loading_msg.delete()
            await update.message.reply_text(balance_text, parse_mode="HTML")

        except Exception as e:
            # Delete loading message if exists
            if loading_msg:
                try:
                    await loading_msg.delete()
                except:
                    pass

            import traceback
            error_detail = traceback.format_exc()
            await update.message.reply_text(f"❌ Error: {e}\n\nDetails:\n<code>{error_detail[:500]}</code>", parse_mode="HTML")

    async def cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /trades [hours] command."""
        try:
            # Parse hours argument (default: 1)
            hours = 1
            if context.args and len(context.args) > 0:
                try:
                    hours = int(context.args[0])
                    if hours <= 0 or hours > 168:  # Max 1 week
                        await update.message.reply_text("❌ Hours must be between 1 and 168")
                        return
                except ValueError:
                    await update.message.reply_text("❌ Invalid hours value")
                    return

            with pg_conn() as conn, conn.cursor() as cur:
                # Get trades in the time window
                cur.execute(
                    """
                    SELECT ts, direction, mm_best_bps, notional_usd, status,
                           request_json, response_json
                    FROM trades
                    WHERE ts > NOW() - INTERVAL '%s hours'
                    ORDER BY ts DESC
                    LIMIT 50
                    """,
                    (hours,)
                )
                trades = cur.fetchall()

                # Get summary stats
                cur.execute(
                    """
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE status = 'POSTED') as posted,
                        COUNT(*) FILTER (WHERE status = 'ERROR') as error,
                        AVG(mm_best_bps) FILTER (WHERE status = 'POSTED') as avg_edge,
                        SUM(notional_usd) FILTER (WHERE status = 'POSTED') as volume
                    FROM trades
                    WHERE ts > NOW() - INTERVAL '%s hours'
                    """,
                    (hours,)
                )
                stats = cur.fetchone()

            if not trades:
                await update.message.reply_text(
                    f"📭 No trades in the last {hours}h",
                    parse_mode="HTML"
                )
                return

            total, posted, error, avg_edge, volume = stats
            success_rate = (posted / total * 100) if total > 0 else 0

            # Build response
            response = (
                f"📊 <b>Trades - Last {hours}h</b>\n\n"
                f"✅ Success: {posted}/{total} ({success_rate:.1f}%)\n"
                f"❌ Errors: {error}\n"
                f"📈 Avg Edge: {avg_edge:.2f} bps\n"
                f"💰 Volume: ${volume:.2f}\n\n"
                f"<b>Recent Trades:</b>\n"
            )

            for trade in trades[:10]:  # Show last 10
                ts, direction, edge, notional, status, req_json, resp_json = trade

                status_emoji = "✅" if status == "POSTED" else "❌"
                direction_emoji = "🔴→🟢" if direction == "perp->spot" else "🟢→🔴"

                time_str = ts.strftime("%H:%M:%S")
                response += (
                    f"{status_emoji} {time_str} {direction_emoji} "
                    f"{edge:.1f}bps ${notional:.0f}\n"
                )

            if len(trades) > 10:
                response += f"\n<i>... and {len(trades) - 10} more</i>"

            await update.message.reply_text(response, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /positions command."""
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                # Get open positions
                cur.execute(
                    """
                    SELECT id, opened_at, direction, open_edge_bps,
                           perp_size, spot_size, perp_entry_px, spot_entry_px
                    FROM positions
                    WHERE status = 'OPEN'
                    ORDER BY opened_at DESC
                    """
                )
                open_pos = cur.fetchall()

                # Get recent closed positions
                cur.execute(
                    """
                    SELECT closed_at, direction, open_edge_bps, close_edge_bps,
                           realized_pnl
                    FROM positions
                    WHERE status = 'CLOSED'
                    ORDER BY closed_at DESC
                    LIMIT 5
                    """
                )
                closed_pos = cur.fetchall()

            response = f"📍 <b>Positions</b>\n\n"

            if open_pos:
                response += f"<b>🟢 Open ({len(open_pos)}):</b>\n"
                for pos in open_pos:
                    pos_id, opened_at, direction, edge, perp_sz, spot_sz, perp_px, spot_px = pos
                    age = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
                    direction_emoji = "🔴→🟢" if direction == "perp->spot" else "🟢→🔴"

                    response += (
                        f"#{pos_id} {direction_emoji} {edge:.1f}bps\n"
                        f"  Age: {age:.0f}m | Size: {perp_sz:.2f} HYPE\n"
                        f"  Entry: Perp ${perp_px:.2f} / Spot ${spot_px:.2f}\n\n"
                    )
            else:
                response += "🟢 No open positions\n\n"

            if closed_pos:
                response += f"<b>📊 Recently Closed:</b>\n"
                for pos in closed_pos:
                    closed_at, direction, open_edge, close_edge, pnl = pos
                    direction_emoji = "🔴→🟢" if direction == "perp->spot" else "🟢→🔴"
                    pnl_emoji = "💰" if pnl > 0 else "💸"

                    response += (
                        f"{direction_emoji} {pnl_emoji} ${pnl:.4f} | "
                        f"Open: {open_edge:.1f}bps → Close: {close_edge:.1f}bps\n"
                    )

            await update.message.reply_text(response, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /pnl [hours] command."""
        try:
            # Parse hours argument (default: 24)
            hours = 24
            if context.args and len(context.args) > 0:
                try:
                    hours = int(context.args[0])
                    if hours <= 0 or hours > 720:  # Max 30 days
                        await update.message.reply_text("❌ Hours must be between 1 and 720")
                        return
                except ValueError:
                    await update.message.reply_text("❌ Invalid hours value")
                    return

            with pg_conn() as conn, conn.cursor() as cur:
                # Get PNL from closed positions
                cur.execute(
                    """
                    SELECT
                        COUNT(*) as total_positions,
                        COUNT(*) FILTER (WHERE realized_pnl > 0) as profitable,
                        COUNT(*) FILTER (WHERE realized_pnl < 0) as losses,
                        SUM(realized_pnl) as total_pnl,
                        AVG(realized_pnl) as avg_pnl,
                        MAX(realized_pnl) as best_pnl,
                        MIN(realized_pnl) as worst_pnl,
                        AVG(open_edge_bps) as avg_open_edge,
                        AVG(close_edge_bps) as avg_close_edge
                    FROM positions
                    WHERE status = 'CLOSED'
                      AND closed_at > NOW() - INTERVAL '%s hours'
                    """,
                    (hours,)
                )
                pnl_stats = cur.fetchone()

                # Get trade stats
                cur.execute(
                    """
                    SELECT
                        COUNT(*) as total_trades,
                        COUNT(*) FILTER (WHERE status = 'POSTED') as successful,
                        SUM(notional_usd) FILTER (WHERE status = 'POSTED') as volume
                    FROM trades
                    WHERE ts > NOW() - INTERVAL '%s hours'
                    """,
                    (hours,)
                )
                trade_stats = cur.fetchone()

            total_pos, profitable, losses, total_pnl, avg_pnl, best, worst, avg_open, avg_close = pnl_stats
            total_trades, successful, volume = trade_stats

            if total_pos == 0:
                await update.message.reply_text(
                    f"📭 No closed positions in the last {hours}h",
                    parse_mode="HTML"
                )
                return

            win_rate = (profitable / total_pos * 100) if total_pos > 0 else 0
            pnl_emoji = "💰" if total_pnl > 0 else "💸" if total_pnl < 0 else "➖"
            roi = (total_pnl / volume * 100) if volume and volume > 0 else 0

            response = (
                f"📊 <b>PNL Report - Last {hours}h</b>\n\n"
                f"{pnl_emoji} <b>Total PNL: ${total_pnl:.4f}</b>\n"
                f"📈 ROI: {roi:.3f}%\n\n"
                f"<b>Positions:</b>\n"
                f"  Total: {total_pos}\n"
                f"  Profitable: {profitable} ({win_rate:.1f}%)\n"
                f"  Losses: {losses}\n"
                f"  Avg PNL: ${avg_pnl:.4f}\n"
                f"  Best: ${best:.4f}\n"
                f"  Worst: ${worst:.4f}\n\n"
                f"<b>Edges:</b>\n"
                f"  Avg Open: {avg_open:.2f} bps\n"
                f"  Avg Close: {avg_close:.2f} bps\n"
                f"  Edge Decay: {avg_open - avg_close:.2f} bps\n\n"
                f"<b>Trading:</b>\n"
                f"  Trades: {successful}/{total_trades}\n"
                f"  Volume: ${volume:.2f}"
            )

            await update.message.reply_text(response, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command - overall statistics."""
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                # All-time stats
                cur.execute(
                    """
                    SELECT
                        COUNT(*) as total_trades,
                        COUNT(*) FILTER (WHERE status = 'POSTED') as posted,
                        COUNT(*) FILTER (WHERE status = 'ERROR') as errors,
                        MIN(ts) as first_trade,
                        MAX(ts) as last_trade
                    FROM trades
                    """
                )
                trade_stats = cur.fetchone()

                cur.execute(
                    """
                    SELECT
                        COUNT(*) as total_positions,
                        COUNT(*) FILTER (WHERE status = 'CLOSED') as closed,
                        SUM(realized_pnl) FILTER (WHERE status = 'CLOSED') as total_pnl
                    FROM positions
                    """
                )
                pos_stats = cur.fetchone()

                # Today vs yesterday
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE ts > NOW() - INTERVAL '24 hours' AND status = 'POSTED') as today_trades,
                        COUNT(*) FILTER (WHERE ts BETWEEN NOW() - INTERVAL '48 hours' AND NOW() - INTERVAL '24 hours' AND status = 'POSTED') as yesterday_trades
                    FROM trades
                    """
                )
                daily_stats = cur.fetchone()

            total_trades, posted, errors, first_trade, last_trade = trade_stats
            total_pos, closed_pos, total_pnl = pos_stats
            today_trades, yesterday_trades = daily_stats

            success_rate = (posted / total_trades * 100) if total_trades > 0 else 0
            uptime_days = (last_trade - first_trade).total_seconds() / 86400 if first_trade and last_trade else 0

            response = (
                f"📈 <b>All-Time Statistics</b>\n\n"
                f"<b>Trading:</b>\n"
                f"  Total Trades: {total_trades}\n"
                f"  Success Rate: {success_rate:.1f}%\n"
                f"  Errors: {errors}\n\n"
                f"<b>Positions:</b>\n"
                f"  Total: {total_pos}\n"
                f"  Closed: {closed_pos}\n"
                f"  Total PNL: ${total_pnl:.4f}\n\n"
                f"<b>Activity:</b>\n"
                f"  Today: {today_trades} trades\n"
                f"  Yesterday: {yesterday_trades} trades\n"
                f"  Running: {uptime_days:.1f} days\n\n"
                f"<b>Config:</b>\n"
                f"  Threshold: {settings.threshold_bps} bps\n"
                f"  Size: ${settings.alloc_per_trade_usd}\n"
                f"  Mode: {'DRY RUN' if settings.dry_run else 'LIVE'}"
            )

            await update.message.reply_text(response, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_rebalance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /rebalance command."""
        try:
            await update.message.reply_text("🔍 Checking balances...")

            # Run rebalance (blocking, but in executor)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                rebalance_capital_sync,
                5.0,  # min_transfer_usd
                settings.dry_run  # dry_run
            )

            balances = result.get("balances", {})
            actions = result.get("actions", {})
            execution = result.get("execution")

            response = (
                f"⚖️ <b>Rebalance Check (50-50 target)</b>\n\n"
                f"💰 Total: ${actions.get('total_value_usdc', 0):.2f}\n"
                f"🎯 Target Perp: ${actions.get('target_perp_usdc', 0):.2f}\n"
                f"🎯 Target Spot: ${actions.get('target_spot_usdc', 0):.2f}\n\n"
            )

            if not actions.get("needs_rebalance"):
                response += "✅ Already balanced, no action needed"
            else:
                response += "<b>Actions Needed:</b>\n"

                if abs(actions.get("perp_to_spot_usdc", 0)) > 5:
                    direction = "Perp → Spot" if actions["perp_to_spot_usdc"] > 0 else "Spot → Perp"
                    response += f"  💸 ${abs(actions['perp_to_spot_usdc']):.2f} USDC ({direction})\n"

                if abs(actions.get("spot_buy_hype_usdc", 0)) > 5:
                    action = "Buy" if actions["spot_buy_hype_usdc"] > 0 else "Sell"
                    response += f"  🔄 {action} HYPE (${abs(actions['spot_buy_hype_usdc']):.2f})\n"

                if settings.dry_run:
                    response += "\n⚠️ DRY RUN mode - no actual execution"
                elif execution:
                    response += "\n✅ Executed!"
                    if execution.get("usdc_transfer"):
                        response += "\n  ✓ USDC transfer done"
                    if execution.get("hype_trade"):
                        response += "\n  ✓ HYPE trade done"

            await update.message.reply_text(response, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_stop_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stopbot command - pause trading."""
        try:
            trading_state = get_trading_state()
            if not trading_state:
                await update.message.reply_text("❌ Trading state not initialized")
                return

            if not trading_state.is_running():
                await update.message.reply_text("ℹ️  Bot is already stopped")
                return

            trading_state.stop()
            await update.message.reply_text(
                "🛑 <b>Trading Stopped</b>\n\n"
                "Bot will no longer execute trades.\n"
                "Use /start_trade to resume.",
                parse_mode="HTML"
            )

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_start_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /startbot command - resume trading."""
        try:
            trading_state = get_trading_state()
            if not trading_state:
                await update.message.reply_text("❌ Trading state not initialized")
                return

            if trading_state.is_running():
                await update.message.reply_text("ℹ️  Bot is already running")
                return

            trading_state.start()
            await update.message.reply_text(
                "🟢 <b>Trading Resumed</b>\n\n"
                "Bot is now actively monitoring for arbitrage opportunities.",
                parse_mode="HTML"
            )

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_edges(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /edges command - show live edge values."""
        try:
            trading_state = get_trading_state()
            if not trading_state:
                await update.message.reply_text("❌ Trading state not initialized")
                return

            edges = trading_state.get_last_edges()

            if not edges:
                await update.message.reply_text("📭 No recent edge data available")
                return

            ps_mm = edges.get("ps_mm", 0)
            sp_mm = edges.get("sp_mm", 0)
            mid_ref = edges.get("mid_ref", 0)
            timestamp = edges.get("timestamp", 0)

            # Calculate age
            import time
            age_seconds = time.time() - timestamp
            age_str = f"{age_seconds:.1f}s ago" if age_seconds < 60 else f"{age_seconds/60:.1f}m ago"

            # Get current threshold
            runtime_config = get_runtime_config()
            threshold = runtime_config.get("threshold_bps", settings.threshold_bps) if runtime_config else settings.threshold_bps

            # Determine which direction is better
            best_direction = "perp→spot" if ps_mm >= sp_mm else "spot→perp"
            best_edge = max(ps_mm, sp_mm)
            best_emoji = "🔴→🟢" if ps_mm >= sp_mm else "🟢→🔴"

            # Check if above threshold
            signal = ""
            if best_edge >= threshold:
                signal = "\n🔥 <b>SIGNAL!</b> Edge above threshold!"

            response = (
                f"📊 <b>Live Edges</b>\n\n"
                f"🔴→🟢 <b>Perp → Spot:</b> {ps_mm:.2f} bps\n"
                f"🟢→🔴 <b>Spot → Perp:</b> {sp_mm:.2f} bps\n\n"
                f"{best_emoji} <b>Best:</b> {best_direction} ({best_edge:.2f} bps)\n"
                f"🎯 <b>Threshold:</b> {threshold:.2f} bps\n"
                f"💰 <b>Mid Price:</b> ${mid_ref:.2f}\n"
                f"⏰ <b>Updated:</b> {age_str}"
                f"{signal}"
            )

            await update.message.reply_text(response, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /config command - show current settings."""
        try:
            runtime_config = get_runtime_config()
            trading_state = get_trading_state()

            # Get current values (runtime overrides or defaults)
            if runtime_config:
                threshold = runtime_config.get("threshold_bps", settings.threshold_bps)
                dry_run = runtime_config.get("dry_run", settings.dry_run)
                spike_extra = runtime_config.get("spike_extra_bps_for_ioc", settings.spike_extra_bps_for_ioc)
                alloc = runtime_config.get("alloc_per_trade_usd", settings.alloc_per_trade_usd)
            else:
                threshold = settings.threshold_bps
                dry_run = settings.dry_run
                spike_extra = settings.spike_extra_bps_for_ioc
                alloc = settings.alloc_per_trade_usd

            use_ioc = spike_extra == 0
            bot_state = trading_state.get_state() if trading_state else "unknown"
            state_emoji = "🟢" if bot_state == "running" else "🔴"

            response = (
                f"⚙️ <b>Current Configuration</b>\n\n"
                f"<b>Bot State:</b>\n"
                f"  {state_emoji} Status: {bot_state.upper()}\n"
                f"  🧪 Dry Run: {'ON' if dry_run else 'OFF'}\n\n"
                f"<b>Trading Parameters:</b>\n"
                f"  🎯 Threshold: {threshold} bps\n"
                f"  💰 Alloc per trade: ${alloc}\n"
                f"  ⚡ IOC Mode: {'ON' if use_ioc else 'OFF'}\n"
                f"  📊 Spike Extra: {spike_extra} bps\n\n"
                f"<b>Pair:</b>\n"
                f"  {settings.pair_base}/{settings.pair_quote}\n\n"
                f"<i>Use /set to change settings</i>"
            )

            await update.message.reply_text(response, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /set command - change settings."""
        try:
            if not context.args or len(context.args) < 2:
                help_text = (
                    "⚙️ <b>Set Command Usage</b>\n\n"
                    "<b>Available settings:</b>\n"
                    "• /set threshold &lt;value&gt; - Set threshold BPS\n"
                    "• /set dryrun &lt;on/off&gt; - Toggle dry run\n"
                    "• /set ioc &lt;on/off&gt; - Toggle IOC mode\n"
                    "• /set alloc &lt;value&gt; - Set trade size (USD)\n\n"
                    "<i>Examples:</i>\n"
                    "• /set threshold 15\n"
                    "• /set dryrun on\n"
                    "• /set ioc off"
                )
                await update.message.reply_text(help_text, parse_mode="HTML")
                return

            setting = context.args[0].lower()
            value = context.args[1].lower()

            runtime_config = get_runtime_config()
            if not runtime_config:
                await update.message.reply_text("❌ Runtime config not initialized")
                return

            if setting == "threshold":
                try:
                    new_threshold = float(value)
                    if new_threshold < 0 or new_threshold > 1000:
                        await update.message.reply_text("❌ Threshold must be between 0 and 1000 bps")
                        return

                    runtime_config.set("threshold_bps", new_threshold)
                    await update.message.reply_text(
                        f"✅ <b>Threshold Updated</b>\n\n"
                        f"New threshold: {new_threshold} bps",
                        parse_mode="HTML"
                    )

                except ValueError:
                    await update.message.reply_text("❌ Invalid value. Must be a number.")

            elif setting == "dryrun":
                if value in ["on", "true", "1", "yes"]:
                    runtime_config.set("dry_run", True)
                    await update.message.reply_text(
                        "✅ <b>Dry Run Enabled</b>\n\n"
                        "Bot will simulate trades without executing.",
                        parse_mode="HTML"
                    )
                elif value in ["off", "false", "0", "no"]:
                    runtime_config.set("dry_run", False)
                    await update.message.reply_text(
                        "✅ <b>Dry Run Disabled</b>\n\n"
                        "⚠️ Bot will now execute REAL trades!",
                        parse_mode="HTML"
                    )
                else:
                    await update.message.reply_text("❌ Invalid value. Use: on/off, true/false")

            elif setting == "ioc":
                if value in ["on", "true", "1", "yes"]:
                    runtime_config.set("spike_extra_bps_for_ioc", 0)
                    await update.message.reply_text(
                        "✅ <b>IOC Mode Enabled</b>\n\n"
                        "All orders will use IOC (Immediate-or-Cancel).",
                        parse_mode="HTML"
                    )
                elif value in ["off", "false", "0", "no"]:
                    runtime_config.set("spike_extra_bps_for_ioc", 7)
                    await update.message.reply_text(
                        "✅ <b>IOC Mode Disabled</b>\n\n"
                        "Orders will use ALO (post-only) by default.",
                        parse_mode="HTML"
                    )
                else:
                    await update.message.reply_text("❌ Invalid value. Use: on/off, true/false")

            elif setting == "alloc":
                try:
                    new_alloc = float(value)
                    if new_alloc < 10 or new_alloc > 10000:
                        await update.message.reply_text("❌ Allocation must be between $10 and $10000")
                        return

                    runtime_config.set("alloc_per_trade_usd", new_alloc)
                    await update.message.reply_text(
                        f"✅ <b>Trade Size Updated</b>\n\n"
                        f"New allocation: ${new_alloc} per trade",
                        parse_mode="HTML"
                    )

                except ValueError:
                    await update.message.reply_text("❌ Invalid value. Must be a number.")

            else:
                await update.message.reply_text(
                    f"❌ Unknown setting: {setting}\n\n"
                    f"Available: threshold, dryrun, ioc, alloc"
                )

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    # ===== NOTIFICATION HELPERS =====

    async def notify_trade(self, direction: str, edge_bps: float, status: str, notional: float, details: str = ""):
        """Send trade notification."""
        status_emoji = "✅" if status == "POSTED" else "❌"
        direction_emoji = "🔴→🟢" if direction == "perp->spot" else "🟢→🔴"

        text = (
            f"{status_emoji} <b>Trade {status}</b>\n"
            f"{direction_emoji} {direction}\n"
            f"📊 Edge: {edge_bps:.2f} bps\n"
            f"💰 Size: ${notional:.2f}\n"
        )

        if details:
            text += f"\n{details}"

        await self.send_message(text)

    async def notify_position_closed(self, direction: str, open_edge: float, close_edge: float, pnl: float, duration_mins: int):
        """Send position closed notification."""
        pnl_emoji = "💰" if pnl > 0 else "💸"
        direction_emoji = "🔴→🟢" if direction == "perp->spot" else "🟢→🔴"

        text = (
            f"{pnl_emoji} <b>Position Closed</b>\n"
            f"{direction_emoji} {direction}\n"
            f"⏱ Duration: {duration_mins}m\n"
            f"📊 Edge: {open_edge:.2f} → {close_edge:.2f} bps\n"
            f"💵 PNL: ${pnl:.4f}"
        )

        await self.send_message(text)

    async def notify_error(self, error_type: str, message: str):
        """Send error notification."""
        text = (
            f"⚠️ <b>{error_type}</b>\n\n"
            f"{message}"
        )

        await self.send_message(text)

    async def notify_rebalance(self, success: bool, message: str):
        """Send rebalance notification."""
        emoji = "✅" if success else "❌"
        text = (
            f"{emoji} <b>Auto-Rebalance</b>\n\n"
            f"{message}"
        )

        await self.send_message(text)

    async def cmd_test_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /test_stats command - Show opportunity tracker statistics."""
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                # Get basic stats
                cur.execute(
                    """
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE volatility_source = 'PERP') as perp_driven,
                        COUNT(*) FILTER (WHERE volatility_source = 'SPOT') as spot_driven,
                        COUNT(*) FILTER (WHERE volatility_source = 'BOTH') as both_driven,
                        AVG(edge_bps) as avg_edge,
                        MIN(detected_at) as first_opp,
                        MAX(detected_at) as last_opp
                    FROM opportunities
                    """
                )
                stats = cur.fetchone()

            if not stats or stats[0] == 0:
                await update.message.reply_text(
                    "📊 <b>Opportunity Tracker</b>\n\n"
                    "🔄 Collecting data...\n\n"
                    "No opportunities tracked yet.\n"
                    "Tracker monitors all 10+ bps opportunities.\n"
                    "Main bot continues trading at 20 bps threshold.",
                    parse_mode="HTML"
                )
                return

            total, perp, spot, both, avg_edge, first_opp, last_opp = stats

            perp_pct = (perp / total * 100) if total > 0 else 0
            spot_pct = (spot / total * 100) if total > 0 else 0
            both_pct = (both / total * 100) if total > 0 else 0

            duration_hours = (last_opp - first_opp).total_seconds() / 3600 if first_opp and last_opp else 0
            opps_per_hour = total / duration_hours if duration_hours > 0 else 0

            response = (
                f"📊 <b>Opportunity Tracker Statistics</b>\n\n"
                f"<b>Collection:</b>\n"
                f"  Total Opportunities: {total}\n"
                f"  Duration: {duration_hours:.1f}h\n"
                f"  Rate: {opps_per_hour:.1f} opps/hour\n"
                f"  Avg Edge: {avg_edge:.2f} bps\n\n"
                f"<b>Volatility Source:</b>\n"
                f"  🔴 PERP-driven: {perp} ({perp_pct:.1f}%)\n"
                f"  🟢 SPOT-driven: {spot} ({spot_pct:.1f}%)\n"
                f"  🟡 BOTH: {both} ({both_pct:.1f}%)\n\n"
                f"<i>Use /test_latest for recent opportunities\n"
                f"Use /test_summary for full analysis</i>"
            )

            await update.message.reply_text(response, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_test_latest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /test_latest command - Show last 5 opportunities."""
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                # Get last 5 opportunities
                cur.execute(
                    """
                    SELECT
                        detected_at,
                        edge_bps,
                        volatility_source,
                        volatility_ratio,
                        perp_movement_bps,
                        spot_movement_bps,
                        expected_profit_adaptive,
                        expected_profit_ioc_both
                    FROM opportunities
                    ORDER BY detected_at DESC
                    LIMIT 5
                    """
                )
                opps = cur.fetchall()

            if not opps:
                await update.message.reply_text(
                    "📭 No opportunities tracked yet.\n\n"
                    "Tracker monitors all 10+ bps opportunities.",
                    parse_mode="HTML"
                )
                return

            response = "🔍 <b>Last 5 Opportunities</b>\n\n"

            for opp in opps:
                detected, edge, source, ratio, perp_mov, spot_mov, profit_adaptive, profit_ioc = opp

                time_str = detected.strftime("%H:%M:%S")
                source_emoji = "🔴" if source == "PERP" else "🟢" if source == "SPOT" else "🟡"

                profit_diff = profit_adaptive - profit_ioc
                profit_symbol = "📈" if profit_diff > 0 else "📉"

                response += (
                    f"{source_emoji} <b>{time_str}</b> | {edge:.1f} bps\n"
                    f"  Source: {source} (ratio: {ratio:.1f}x)\n"
                    f"  Movement: PERP {perp_mov:.1f} / SPOT {spot_mov:.1f} bps\n"
                    f"  {profit_symbol} Adaptive: +{profit_diff:.1f} bps better\n\n"
                )

            await update.message.reply_text(response, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    async def cmd_test_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /test_summary command - Comprehensive analysis summary."""
        try:
            with pg_conn() as conn, conn.cursor() as cur:
                # Get comprehensive stats
                cur.execute(
                    """
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE volatility_source = 'PERP') as perp_driven,
                        COUNT(*) FILTER (WHERE volatility_source = 'SPOT') as spot_driven,
                        AVG(edge_bps) as avg_edge,
                        AVG(expected_profit_ioc_both) as avg_profit_current,
                        AVG(expected_profit_adaptive) as avg_profit_adaptive,
                        AVG(cost_ioc_both) as avg_cost_current,
                        AVG(cost_ioc_perp_alo_spot) FILTER (WHERE volatility_source = 'PERP') as avg_cost_perp_adaptive,
                        AVG(perp_movement_bps) FILTER (WHERE volatility_source = 'PERP') as avg_perp_movement_when_perp,
                        AVG(spot_movement_bps) FILTER (WHERE volatility_source = 'SPOT') as avg_spot_movement_when_spot
                    FROM opportunities
                    """
                )
                stats = cur.fetchone()

            if not stats or stats[0] == 0:
                await update.message.reply_text(
                    "📭 No data collected yet.\n\n"
                    "Collecting 500+ opportunities needed for analysis.\n"
                    "Current threshold: 10+ bps",
                    parse_mode="HTML"
                )
                return

            (total, perp, spot, avg_edge, avg_profit_current, avg_profit_adaptive,
             avg_cost_current, avg_cost_perp_adaptive, avg_perp_mov, avg_spot_mov) = stats

            perp_pct = (perp / total * 100) if total > 0 else 0
            spot_pct = (spot / total * 100) if total > 0 else 0

            # Calculate potential improvement
            profit_diff = avg_profit_adaptive - avg_profit_current if avg_profit_adaptive and avg_profit_current else 0
            improvement_pct = (profit_diff / avg_profit_current * 100) if avg_profit_current and avg_profit_current > 0 else 0

            # Recommendation logic
            if total < 100:
                recommendation = "⏳ <b>Collecting more data...</b>\nNeed 500+ opportunities for confident decision."
            elif perp_pct >= 70 and improvement_pct > 5:
                recommendation = "✅ <b>RECOMMENDED: Implement adaptive strategy</b>\nPERP-driven dominance detected, significant profit improvement expected."
            elif spot_pct >= 60:
                recommendation = "⚠️ <b>KEEP CURRENT STRATEGY</b>\nSPOT-driven majority, current strategy is optimal."
            else:
                recommendation = "🟡 <b>MIXED RESULTS</b>\nNo clear pattern. Collect more data or run A/B test."

            response = (
                f"📊 <b>Opportunity Analysis Summary</b>\n\n"
                f"<b>📈 Data Collection:</b>\n"
                f"  Total Opportunities: {total}\n"
                f"  Avg Edge: {avg_edge:.2f} bps\n\n"
                f"<b>🎯 Volatility Pattern:</b>\n"
                f"  🔴 PERP-driven: {perp_pct:.1f}%\n"
                f"  🟢 SPOT-driven: {spot_pct:.1f}%\n\n"
                f"<b>💰 Profit Projection:</b>\n"
                f"  Current Strategy: {avg_profit_current:.2f} bps avg\n"
                f"  Adaptive Strategy: {avg_profit_adaptive:.2f} bps avg\n"
                f"  Improvement: {profit_diff:+.2f} bps ({improvement_pct:+.1f}%)\n\n"
                f"<b>📉 Cost Analysis:</b>\n"
                f"  Current (IOC both): {avg_cost_current:.2f} bps\n"
            )

            if avg_cost_perp_adaptive:
                response += f"  Adaptive (PERP): {avg_cost_perp_adaptive:.2f} bps\n\n"
            else:
                response += "\n"

            response += f"{recommendation}"

            await update.message.reply_text(response, parse_mode="HTML")

        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            await update.message.reply_text(
                f"❌ Error: {e}\n\n<code>{error_detail[:500]}</code>",
                parse_mode="HTML"
            )

    async def cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /test command - Start A/B testing."""
        try:
            # Import here to avoid circular dependency
            from .ab_tester import ABTester, QUICK_TEST_SCENARIOS

            await update.message.reply_text(
                "🧪 <b>A/B Testing Starting</b>\n\n"
                "Running 3 test scenarios:\n"
                "1. IOC ON, 20 bps (30min)\n"
                "2. IOC OFF, 15 bps (30min)\n"
                "3. IOC OFF, 10 bps (30min)\n\n"
                "Total time: ~90 minutes\n\n"
                "You'll receive updates as each test completes.",
                parse_mode="HTML"
            )

            # Run tests in background
            tester = ABTester(test_duration_minutes=30)
            results = await tester.run_multiple_tests(QUICK_TEST_SCENARIOS)

            # Send final summary
            best = max(results, key=lambda x: x['pnl'])
            summary = "✅ <b>A/B Testing Complete!</b>\n\n"

            for r in results:
                summary += f"<b>{r['scenario']['name']}</b>\n"
                summary += f"PNL: ${r['pnl']:.4f} | Trades: {r['trade_count']}\n\n"

            summary += f"🏆 <b>Winner:</b> {best['scenario']['name']}\n"
            summary += f"Best PNL: ${best['pnl']:.4f}"

            await update.message.reply_text(summary, parse_mode="HTML")

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")


# Global instance
_telegram_notifier: Optional[TelegramNotifier] = None


def get_telegram_notifier() -> Optional[TelegramNotifier]:
    """Get the global Telegram notifier instance."""
    return _telegram_notifier


async def init_telegram_bot(token: str, chat_id: str) -> TelegramNotifier:
    """Initialize and start the Telegram bot."""
    global _telegram_notifier

    if not token or not chat_id:
        print("⚠️  Telegram token/chat_id not configured, notifications disabled")
        return None

    _telegram_notifier = TelegramNotifier(token, chat_id)
    await _telegram_notifier.start_bot()
    return _telegram_notifier


async def stop_telegram_bot():
    """Stop the Telegram bot."""
    global _telegram_notifier
    if _telegram_notifier:
        await _telegram_notifier.stop_bot()
        _telegram_notifier = None
