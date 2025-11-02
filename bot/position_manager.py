"""
Position Manager - Açık pozisyonları takip eder ve kapatma kararları verir.

Kapatma Stratejisi (YENİ):
1. Spread ≤ 0.5 bps → ALO ile kapat
2. 5 dakika ALO bekle (hedge var, risk yok)
3. Hala kapanmadıysa → IOC ile kapat

Bu sayede:
- Kapanışta maker fees (5.5 bps) - ucuz!
- 5dk timeout - risksiz bekleyebiliriz
- IOC fallback - garantili kapanma
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
import json

from .config import settings
from .storage import get_open_positions, close_position
from .hl_client import compute_edges
from .execution import HyperliquidTrader
from .telegram_bot import get_telegram_notifier
from .execution_alo_close import close_with_alo_first


class PositionManager:
    """Açık arbitraj pozisyonlarını yönetir ve otomatik kapatır."""

    def __init__(self, trader: HyperliquidTrader):
        self.trader = trader
        self.check_interval = 1.0  # Her 1 saniyede kontrol et

    async def monitor_positions(self, perp_bid: float, perp_ask: float, spot_bid: float, spot_ask: float):
        """
        Açık pozisyonları kontrol et ve gerekirse kapat.
        Bu fonksiyon strategy'nin her edge update'inde çağrılmalı.
        """
        open_positions = get_open_positions()

        if not open_positions:
            return

        # Mevcut spread'i hesapla
        edges = compute_edges(perp_bid, perp_ask, spot_bid, spot_ask, {
            "perp": {"maker": settings.perp_maker_bps, "taker": settings.perp_taker_bps},
            "spot": {"maker": settings.spot_maker_bps, "taker": settings.spot_taker_bps},
        })

        now = datetime.now(timezone.utc)

        for pos in open_positions:
            pos_id, opened_at, base, direction, open_edge_bps, perp_size, spot_size, perp_entry_px, spot_entry_px, timeout_seconds = pos

            # Timeout kontrolü
            time_elapsed = (now - opened_at).total_seconds()
            is_timeout = time_elapsed >= timeout_seconds

            # Spread kontrolü - direction'a göre doğru edge'i seç
            if direction == "perp->spot":
                current_edge = edges["ps_mm"]
            else:
                current_edge = edges["sp_mm"]

            # Kapatma koşulları
            should_close = False
            close_reason = ""

            # Koşul 1: Spread neredeyse sıfır (≤ 0.5 bps)
            if current_edge <= 0.5:
                should_close = True
                close_reason = f"spread_closed (edge: {current_edge:.2f} bps)"

            # Koşul 2: Timeout (5 dakika geçti)
            elif is_timeout:
                should_close = True
                close_reason = f"timeout ({time_elapsed:.0f}s / {timeout_seconds}s)"

            if should_close:
                print(f"🔴 Closing position {pos_id}: {close_reason}")
                await self._close_position(
                    pos_id, direction, perp_size, spot_size,
                    perp_bid, perp_ask, spot_bid, spot_ask,
                    current_edge, perp_entry_px, spot_entry_px,
                    open_edge_bps, opened_at
                )

    async def _close_position(
        self,
        pos_id: int,
        direction: str,
        perp_size: float,
        spot_size: float,
        perp_bid: float,
        perp_ask: float,
        spot_bid: float,
        spot_ask: float,
        current_edge: float,
        perp_entry_px: float,
        spot_entry_px: float,
        open_edge_bps: float,
        opened_at: datetime
    ):
        """
        Pozisyonu kapat: Perp pozisyonu kapat + Spot HYPE sat
        """
        try:
            # Ters yönde order açarak pozisyonu kapat
            # perp->spot açtıysak (short + buy), kapanış: long + sell
            # spot->perp açtıysak (long + sell), kapanış: short + buy

            close_direction = "spot->perp" if direction == "perp->spot" else "perp->spot"

            # 🎯 YENİ KAPATMA STRATEJİSİ: ALO-First + 5dk timeout + IOC fallback
            print(f"  🎯 Using ALO-first close strategy...")
            print(f"     Original direction: {direction}")
            print(f"     Close direction: {close_direction}")

            # Get Info instance from trader
            from hyperliquid.info import Info
            info = Info(self.trader._base_url, skip_ws=True)

            result = await close_with_alo_first(
                trader=self.trader,
                info=info,
                wallet_address=settings.master_wallet if settings.master_wallet else self.trader._wallet.address,
                direction=close_direction,  # 🔧 FIX: Use close_direction, not original direction!
                size=perp_size,  # Use perp size (should match spot)
                perp_bid=perp_bid,
                perp_ask=perp_ask,
                spot_bid=spot_bid,
                spot_ask=spot_ask,
                alo_timeout_seconds=900  # 15 minutes (safe because position is hedged)
            )

            if result.get("ok"):
                # Kar hesaplama
                # perp->spot açıldıysa: short perp (entry'de sattık), long spot (entry'de aldık)
                # Kapatırken: long perp (şimdi alıyoruz), short spot (şimdi satıyoruz)

                if direction == "perp->spot":
                    # Short perp kapatma: buy @ perp_ask
                    # Spot HYPE satış: sell @ spot_bid
                    perp_exit_px = perp_ask
                    spot_exit_px = spot_bid
                    perp_pnl = (perp_entry_px - perp_exit_px) * perp_size  # Short: giriş-çıkış
                    spot_pnl = (spot_exit_px - spot_entry_px) * spot_size  # Long: çıkış-giriş
                else:
                    # Long perp kapatma: sell @ perp_bid
                    # Spot HYPE alım: buy @ spot_ask
                    perp_exit_px = perp_bid
                    spot_exit_px = spot_ask
                    perp_pnl = (perp_exit_px - perp_entry_px) * perp_size  # Long: çıkış-giriş
                    spot_pnl = (spot_entry_px - spot_exit_px) * spot_size  # Short: giriş-çıkış

                # Calculate fees
                # Opening trade fees (from trade execution)
                perp_notional_entry = perp_entry_px * perp_size
                spot_notional_entry = spot_entry_px * spot_size

                # Closing trade fees (depends on close method used)
                perp_notional_exit = perp_exit_px * perp_size
                spot_notional_exit = spot_exit_px * spot_size

                # Use maker or taker fees based on actual close method
                close_method = result.get("method", "unknown")
                if close_method == "alo":
                    # Maker fees for closing (ALO succeeded)
                    perp_fee_exit = perp_notional_exit * (settings.perp_maker_bps / 10000)
                    spot_fee_exit = spot_notional_exit * (settings.spot_maker_bps / 10000)
                else:
                    # Taker fees for closing (IOC fallback)
                    perp_fee_exit = perp_notional_exit * (settings.perp_taker_bps / 10000)
                    spot_fee_exit = spot_notional_exit * (settings.spot_taker_bps / 10000)

                # Opening fees - IOC always (new strategy)
                perp_fee_entry = perp_notional_entry * (settings.perp_taker_bps / 10000)
                spot_fee_entry = spot_notional_entry * (settings.spot_taker_bps / 10000)

                total_fees = perp_fee_entry + spot_fee_entry + perp_fee_exit + spot_fee_exit

                # Real PNL = price difference - fees
                total_pnl = perp_pnl + spot_pnl - total_fees

                # Database'e kaydet
                close_position(
                    pos_id,
                    datetime.now(timezone.utc),
                    current_edge,
                    perp_exit_px,
                    spot_exit_px,
                    total_pnl
                )

                gross_pnl = perp_pnl + spot_pnl
                close_method = result.get("method", "unknown")
                alo_duration = result.get("alo_duration_seconds", 0)

                print(f"✅ Position {pos_id} closed successfully!")
                print(f"   Close method: {close_method}")
                if close_method == "alo" and alo_duration:
                    print(f"   ALO fill time: {alo_duration:.1f}s")
                print(f"   Gross PnL: ${gross_pnl:.4f} (Perp: ${perp_pnl:.4f}, Spot: ${spot_pnl:.4f})")
                print(f"   Total Fees: ${total_fees:.4f}")
                print(f"   Net PnL: ${total_pnl:.4f}")
                print(f"   Close edge: {current_edge:.2f} bps")

                # Notify via Telegram
                telegram = get_telegram_notifier()
                if telegram:
                    duration_mins = int((datetime.now(timezone.utc) - opened_at).total_seconds() / 60)
                    await telegram.notify_position_closed(
                        direction, open_edge_bps, current_edge, total_pnl, duration_mins
                    )
            else:
                print(f"❌ Failed to close position {pos_id}: {result.get('response', {})}")

        except Exception as e:
            print(f"❌ Error closing position {pos_id}: {e}")
            import traceback
            traceback.print_exc()
