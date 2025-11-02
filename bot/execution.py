import asyncio
import itertools
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Dict, List, Optional, Sequence, Tuple

from eth_account import Account

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils.signing import OrderType as HLOrderType
from hyperliquid.utils.signing import (
    ScheduleCancelAction,
    get_timestamp_ms,
    order_request_to_order_wire,
    order_wires_to_order_action,
    sign_l1_action,
)

from .config import settings


def _quantize(value: float, decimals: int) -> float:
    """
    Quantize a float value to a fixed number of decimals using Decimal to avoid
    binary rounding issues that would otherwise break float_to_wire.
    """
    if decimals < 0:
        raise ValueError("decimals must be non-negative")
    quant = Decimal("1").scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN))

def _quantize_up(value: float, decimals: int) -> float:
    if decimals < 0:
        raise ValueError("decimals must be non-negative")
    quant = Decimal("1").scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_UP))


class WsPostSession:
    """
    Helper around a live Hyperliquid websocket connection to support `post` calls.
    """

    def __init__(self, ws):
        self._ws = ws
        self._pending: Dict[str, asyncio.Future] = {}
        self._id_iter = itertools.count(1)
        self._lock = asyncio.Lock()
        self._closed = False

    def close(self, exc: Optional[BaseException] = None) -> None:
        """
        Mark the session closed and reject all pending requests.
        """
        if self._closed:
            return
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc or RuntimeError("websocket closed"))
        self._pending.clear()

    async def post(self, request: Dict[str, Any], timeout: float = 2.0) -> Dict[str, Any]:
        """
        Send a `post` request and await the response payload.
        """
        if self._closed:
            raise RuntimeError("post session closed")
        msg_id = next(self._id_iter)
        payload = {"method": "post", "id": msg_id, "request": request}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[str(msg_id)] = fut
        async with self._lock:
            await self._ws.send(json.dumps(payload))
        try:
            result = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(str(msg_id), None)
        return {"id": msg_id, "response": result}

    def handle_post_response(self, data: Dict[str, Any]) -> None:
        """
        Resolve the future attached to a previously sent `post` call.
        """
        msg_id = str(data.get("id"))
        fut = self._pending.get(msg_id)
        if fut and not fut.done():
            fut.set_result(data.get("response"))


@dataclass
class OrderSpec:
    coin: str
    is_buy: bool
    size: float
    limit_px: float
    tif: str  # "Alo" (maker) or "Ioc"
    reduce_only: bool = False  # True = only close existing positions


class HyperliquidTrader:
    """
    Builds and signs Hyperliquid order actions and dispatches them over an
    attached websocket `post` session.
    """

    def __init__(self):
        if not settings.api_privkey:
            raise RuntimeError("API private key not configured")
        key = settings.api_privkey[2:] if settings.api_privkey.startswith("0x") else settings.api_privkey
        self._wallet = Account.from_key(bytes.fromhex(key))
        base_url = settings.hl_info_url.replace("/info", "")
        self._info = Info(base_url, skip_ws=True)
        self._is_mainnet = base_url.endswith('hyperliquid.xyz')
        self._base_url = base_url

        self._perp_name = settings.pair_base
        self._spot_symbol = f"{settings.pair_base}/{settings.pair_quote}"
        spot_coin = self._info.name_to_coin.get(self._spot_symbol)
        if spot_coin is None:
            raise RuntimeError(f"Could not resolve spot coin for {self._spot_symbol}")

        self._spot_coin = spot_coin
        self._session: Optional[WsPostSession] = None

        self._perp_asset = self._info.name_to_asset(self._perp_name)
        self._spot_asset = self._info.name_to_asset(self._spot_coin)
        self._perp_sz_decimals = self._info.asset_to_sz_decimals[self._perp_asset]
        self._spot_sz_decimals = self._info.asset_to_sz_decimals[self._spot_asset]

        # Get price decimals from meta
        meta = self._info.meta()
        self._perp_px_decimals = 5  # Default
        self._spot_px_decimals = 2  # Default

        for asset_info in meta.get('universe', []):
            if asset_info.get('name') == self._perp_name:
                self._perp_px_decimals = asset_info.get('szDecimals', 5)

        # Get spot px_decimals from spotMeta
        spot_meta = self._info.post('/info', {'type': 'spotMeta'})
        for universe_item in spot_meta.get('universe', []):
            if universe_item.get('name') == self._spot_symbol:
                self._spot_px_decimals = universe_item.get('szDecimals', 2)
                break

        # Store recent prices to estimate clip size.
        self._last_perp_mid = None
        self._last_spot_mid = None

        # Set leverage for perp trading
        self._set_leverage()

    def _set_leverage(self) -> None:
        """
        Set leverage for perpetual trading on Hyperliquid.
        This must be done before opening positions.
        """
        try:
            from hyperliquid.exchange import Exchange
            ex = Exchange(self._wallet, base_url=self._base_url, meta=None, spot_meta=None)

            # Set leverage for the perpetual asset
            result = ex.update_leverage(
                settings.leverage,  # e.g., 3
                self._perp_name,    # e.g., "HYPE"
                is_cross=True       # Use cross margin (safer)
            )

            print(f"✅ Leverage set to {settings.leverage}x for {self._perp_name}: {result}")
        except Exception as e:
            print(f"⚠️  Failed to set leverage: {e}")
            print(f"   Continuing anyway - leverage may already be set")

    def attach_session(self, session: Optional[WsPostSession]) -> None:
        """
        Update the websocket session. Passing None detaches the trader.
        """
        self._session = session

    def update_mid_prices(self, perp_bid: float, perp_ask: float, spot_bid: float, spot_ask: float) -> None:
        """
        Keep track of most recent mid prices for sizing.
        """
        self._last_perp_mid = (perp_bid + perp_ask) / 2
        self._last_spot_mid = (spot_bid + spot_ask) / 2

    @property
    def ready(self) -> bool:
        return self._session is not None

    def _build_order_specs(
        self,
        direction: str,
        use_ioc: bool,
        perp_bid: float,
        perp_ask: float,
        spot_bid: float,
        spot_ask: float,
        reduce_only: bool = False,
    ) -> List[OrderSpec]:
        tif = "Ioc" if use_ioc else "Alo"
        orders: List[OrderSpec] = []

        # 🔧 FIX: Account for leverage in perp size calculation
        # Perp uses leverage, spot does not
        # For proper hedge: perp_notional = margin × leverage
        target_margin = max(settings.alloc_per_trade_usd, settings.min_order_notional_usd)
        perp_notional = target_margin * settings.leverage  # e.g., $12 × 3 = $36
        spot_notional = target_margin  # Spot has no leverage, use margin amount

        # Derive sizes from mid prices to avoid divide-by-zero.
        perp_ref = self._last_perp_mid or (perp_bid + perp_ask) / 2
        spot_ref = self._last_spot_mid or (spot_bid + spot_ask) / 2
        if perp_ref <= 0 or spot_ref <= 0:
            raise RuntimeError("Invalid reference price for sizing")

        # Calculate sizes based on leveraged notionals
        # Both should be same token amount for proper hedge
        perp_size = _quantize_up(perp_notional / perp_ref, self._perp_sz_decimals)
        spot_size = _quantize_up(perp_notional / spot_ref, self._spot_sz_decimals)  # Use perp_notional for hedge

        if perp_size <= 0 or spot_size <= 0:
            raise RuntimeError("Calculated trade size is zero")

        print(f"📐 Size calculation: margin=${target_margin}, leverage={settings.leverage}x")
        print(f"   Perp notional: ${perp_notional:.2f} → {perp_size} {self._perp_name}")
        print(f"   Spot hedge: {spot_size} {self._perp_name} (matches perp for proper hedge)")

        if direction == "perp->spot":
            # 🔵 perp->spot: ps_mm edge positive
            # ps_mm = (perp_bid - spot_ask) > 0 → Perp expensive, Spot cheap
            # Arbitrage: SELL perp (SHORT) at high perp_bid, BUY spot at low spot_ask
            # Action: PERP SHORT + SPOT BUY
            if use_ioc:
                # IOC: Cross spread - be aggressive
                # Sell perp (SHORT): go BELOW bid to guarantee fill
                # Buy spot: go ABOVE ask to guarantee fill
                perp_px = _quantize(perp_bid * 0.9995, self._perp_px_decimals)  # 0.05% below bid
                spot_px = _quantize_up(spot_ask * 1.0005, self._spot_px_decimals)  # 0.05% above ask
            else:
                # ALO: Passive pricing (inside spread)
                perp_px = _quantize(perp_ask, self._perp_px_decimals)  # Sell at ask (passive)
                spot_px = _quantize(spot_bid, self._spot_px_decimals)  # Buy at bid (passive)
            orders.append(OrderSpec(self._perp_name, False, perp_size, perp_px, tif, reduce_only))  # SELL = SHORT
            orders.append(OrderSpec(self._spot_coin, True, spot_size, spot_px, tif, reduce_only))  # BUY
        else:
            # 🔴 spot->perp: sp_mm edge positive
            # sp_mm = (spot_bid - perp_ask) > 0 → Spot expensive, Perp cheap
            # Arbitrage: SELL spot at high spot_bid, BUY perp (LONG) at low perp_ask
            # Action: SPOT SELL + PERP LONG
            if use_ioc:
                # IOC: Cross spread - be aggressive
                # Sell spot: go BELOW bid to guarantee fill
                # Buy perp (LONG): go ABOVE ask to guarantee fill
                spot_px = _quantize(spot_bid * 0.9995, self._spot_px_decimals)  # 0.05% below bid
                perp_px = _quantize_up(perp_ask * 1.0005, self._perp_px_decimals)  # 0.05% above ask
            else:
                # ALO: Passive pricing (inside spread)
                spot_px = _quantize(spot_ask, self._spot_px_decimals)  # Sell at ask (passive)
                perp_px = _quantize(perp_bid, self._perp_px_decimals)  # Buy at bid (passive)
            orders.append(OrderSpec(self._perp_name, True, perp_size, perp_px, tif, reduce_only))  # BUY = LONG
            orders.append(OrderSpec(self._spot_coin, False, spot_size, spot_px, tif, reduce_only))  # SELL

        return orders

    def _build_action(self, orders: Sequence[OrderSpec]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        order_requests = []
        for order in orders:
            order_requests.append(
                {
                    "coin": order.coin,
                    "is_buy": order.is_buy,
                    "sz": order.size,
                    "limit_px": order.limit_px,
                    "order_type": {"limit": {"tif": order.tif}},
                    "reduce_only": order.reduce_only,  # ✅ FIX: Use order's reduce_only flag
                }
            )
        order_wires = [
            order_request_to_order_wire(order_req, self._info.name_to_asset(order_req["coin"]))
            for order_req in order_requests
        ]
        action = order_wires_to_order_action(order_wires)
        nonce = get_timestamp_ms()
        signature = sign_l1_action(
            self._wallet,
            action,
            None,
            nonce,
            None,
            self._is_mainnet,
        )
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }
        return payload, {"orders": order_requests}

    async def execute(
        self,
        direction: str,
        mm_best_bps: float,
        use_ioc: bool,
        perp_bid: float,
        perp_ask: float,
        spot_bid: float,
        spot_ask: float,
        deadman_ms: int,
        reduce_only: bool = False,  # ✅ FIX: Prevent opening wrong positions when closing
    ) -> Dict[str, Any]:
        self.update_mid_prices(perp_bid, perp_ask, spot_bid, spot_ask)
        orders = self._build_order_specs(direction, use_ioc, perp_bid, perp_ask, spot_bid, spot_ask, reduce_only)
        ws_error = None
        if self._session is not None:
            try:
                # Separate orders by asset class (perp vs spot)
                perp_orders = []
                spot_orders = []
                for o in orders:
                    if o.coin == self._perp_name:
                        perp_orders.append(o)
                    else:
                        spot_orders.append(o)

                # Submit perp and spot orders separately
                perp_result = None
                spot_result = None

                if perp_orders:
                    perp_payload, perp_meta = self._build_action(perp_orders)
                    perp_request = {"type": "action", "payload": perp_payload}
                    perp_result = await self._session.post(perp_request, timeout=10.0)

                if spot_orders:
                    spot_payload, spot_meta = self._build_action(spot_orders)
                    spot_request = {"type": "action", "payload": spot_payload}
                    spot_result = await self._session.post(spot_request, timeout=10.0)

                # Check if both orders succeeded
                perp_response = perp_result.get("response") or {} if perp_result else {}
                spot_response = spot_result.get("response") or {} if spot_result else {}

                # 🔧 FIX: More thorough rejection detection
                # Check both "type" != "error" AND response.data.statuses for "rejected"
                def _is_order_ok(response):
                    if not isinstance(response, dict):
                        return False
                    # Check error type
                    if response.get("type") == "error":
                        return False
                    # Check status in response data
                    data = response.get("data", {})
                    if isinstance(data, dict):
                        statuses = data.get("statuses", [])
                        if any(s.get("status") == "rejected" for s in statuses if isinstance(s, dict)):
                            return False
                    return True

                perp_ok = not perp_orders or _is_order_ok(perp_response)
                spot_ok = not spot_orders or _is_order_ok(spot_response)
                ok = perp_ok and spot_ok

                # Print detailed response for debugging
                if perp_orders:
                    print(f"   PERP response: {'✅ OK' if perp_ok else '❌ FAILED'} - {perp_response}")
                if spot_orders:
                    print(f"   SPOT response: {'✅ OK' if spot_ok else '❌ FAILED'} - {spot_response}")

                # 🚨 REJECTED TRADE PROTECTION 🚨
                # If one leg failed but the other succeeded, close the successful leg immediately!
                if not ok and (perp_ok != spot_ok):
                    print("\n⚠️ ⚠️ ⚠️  PARTIAL FILL DETECTED! ⚠️ ⚠️ ⚠️")
                    print(f"   Perp: {'SUCCESS' if perp_ok else 'FAILED'}")
                    print(f"   Spot: {'SUCCESS' if spot_ok else 'FAILED'}")
                    print("   Closing the successful leg to prevent unhedged position...")

                    # Determine which order succeeded and get its size
                    successful_order = None
                    for i, o in enumerate(orders):
                        is_perp = (o.coin == self._perp_name)
                        if (is_perp and perp_ok) or (not is_perp and spot_ok):
                            successful_order = o
                            break

                    if successful_order:
                        print(f"   Closing {successful_order.size} {successful_order.coin}")

                        # 🔧 FIX: Close only the successful leg, not both!
                        # Determine if successful order was perp or spot
                        is_perp_success = (successful_order.coin == self._perp_name)

                        # Close the successful hedge immediately with IOC
                        try:
                            close_result = await self.close_single_leg(
                                is_perp=is_perp_success,
                                is_buy=successful_order.is_buy,  # What we did
                                size=successful_order.size,
                                perp_bid=perp_bid,
                                perp_ask=perp_ask,
                                spot_bid=spot_bid,
                                spot_ask=spot_ask,
                            )

                            if close_result.get("ok"):
                                print("   ✅ Unhedged position closed successfully")
                            else:
                                print("   ❌ Failed to close unhedged position - MANUAL INTERVENTION NEEDED!")
                                print(f"      Close result: {close_result}")

                        except Exception as close_exc:
                            print(f"   ❌ Exception closing hedge: {close_exc}")
                            import traceback
                            traceback.print_exc()
                    else:
                        print("   ❌ Could not determine successful order!")
                        print("   MANUAL INTERVENTION NEEDED - check positions!")

                # Schedule cancel only if orders succeeded
                deadman_result = None
                if ok and not use_ioc and deadman_ms > 0:
                    try:
                        schedule_payload = self._build_schedule_cancel_payload(deadman_ms)
                        deadman_result = await self._session.post(schedule_payload, timeout=10.0)
                        schedule_response = deadman_result.get("response") or {}
                        # Don't fail the entire trade if scheduleCancel fails due to volume requirements
                        if isinstance(schedule_response, dict) and schedule_response.get("type") == "error":
                            error_msg = str(schedule_response)
                            if "volume traded" not in error_msg.lower():
                                ok = False
                    except Exception as schedule_exc:
                        # Log but don't fail the trade if scheduleCancel fails
                        deadman_result = {"error": repr(schedule_exc)}

                if ok or perp_result or spot_result:
                    order_requests = []
                    for o in orders:
                        order_requests.append({
                            "coin": o.coin,
                            "is_buy": o.is_buy,
                            "sz": o.size,
                            "limit_px": o.limit_px,
                            "order_type": {"limit": {"tif": o.tif}},
                            "reduce_only": False,
                        })

                    return {
                        "ok": ok,
                        "mm_best_bps": mm_best_bps,
                        "request": {"direction": direction, "use_ioc": use_ioc, "orders": order_requests},
                        "response": {
                            "perp_order": perp_result,
                            "spot_order": spot_result,
                            "scheduleCancel": deadman_result
                        },
                        "request_id": str(perp_result.get("id")) if perp_result and perp_result.get("id") is not None else None,
                    }
            except Exception as e:
                ws_error = repr(e)

        ex = Exchange(self._wallet, base_url=self._base_url, meta=None, spot_meta=None)
        order_type: HLOrderType = {"limit": {"tif": "Ioc" if use_ioc else "Alo"}}
        spot_orders = []
        perp_orders = []
        for o in orders:
            entry = {
                "coin": (self._spot_symbol if o.coin == self._spot_coin else o.coin),
                "is_buy": o.is_buy,
                "sz": o.size,
                "limit_px": o.limit_px,
                "order_type": order_type,
                "reduce_only": False,
            }
            if entry["coin"] == self._spot_symbol:
                spot_orders.append(entry)
            else:
                perp_orders.append(entry)
        http_resp = {}
        if perp_orders:
            http_resp["perp"] = ex.bulk_orders(perp_orders)
        if spot_orders:
            http_resp["spot"] = ex.bulk_orders(spot_orders)
        http_deadman = None
        if not use_ioc and deadman_ms > 0:
            try:
                http_deadman = ex.schedule_cancel(get_timestamp_ms() + deadman_ms)
            except Exception as schedule_exc:
                # Don't fail the entire trade if scheduleCancel fails
                http_deadman = {"error": repr(schedule_exc)}

        def _is_err(x):
            return isinstance(x, dict) and x.get("type") == "error"
        http_ok = True
        if isinstance(http_resp, dict):
            for k,v in http_resp.items():
                if _is_err(v):
                    http_ok = False

        # Build http_orders list from perp and spot orders
        http_orders = perp_orders + spot_orders

        return {
            "ok": http_ok,
            "mm_best_bps": mm_best_bps,
            "request": {"direction": direction, "use_ioc": use_ioc, "orders": http_orders},
            "response": {"order": http_resp, "scheduleCancel": http_deadman, "ws_error": ws_error},
            "request_id": None,
        }

    def _build_schedule_cancel_payload(self, deadman_ms: int) -> Dict[str, Any]:
        trigger_at = get_timestamp_ms() + deadman_ms
        action: ScheduleCancelAction = {"type": "scheduleCancel", "time": trigger_at}
        nonce = get_timestamp_ms()
        signature = sign_l1_action(
            self._wallet,
            action,
            None,
            nonce,
            None,
            self._is_mainnet,
        )
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }
        return {"type": "action", "payload": payload}

    async def close_single_leg(
        self,
        is_perp: bool,
        is_buy: bool,
        size: float,
        perp_bid: float,
        perp_ask: float,
        spot_bid: float,
        spot_ask: float,
    ) -> Dict[str, Any]:
        """
        Close a single leg (perp OR spot, not both) when partial fill occurs.

        Args:
            is_perp: True if closing perp, False if closing spot
            is_buy: What we did originally (True = bought, False = sold)
            size: Size to close
            perp_bid, perp_ask, spot_bid, spot_ask: Current market prices

        Returns:
            Result dict with 'ok' status
        """
        print(f"⚠️ CLOSING SINGLE LEG: {'PERP' if is_perp else 'SPOT'}, original={'BUY' if is_buy else 'SELL'}")

        tif = "Ioc"
        orders: List[OrderSpec] = []

        if is_perp:
            # Closing perp position
            if is_buy:
                # We bought perp (LONG), now close by selling (SHORT)
                perp_px = _quantize(perp_bid * 0.9995, self._perp_px_decimals)
                orders.append(OrderSpec(self._perp_name, False, size, perp_px, tif, reduce_only=True))
            else:
                # We sold perp (SHORT), now close by buying (LONG)
                perp_px = _quantize_up(perp_ask * 1.0005, self._perp_px_decimals)
                orders.append(OrderSpec(self._perp_name, True, size, perp_px, tif, reduce_only=True))
        else:
            # Closing spot position
            if is_buy:
                # We bought spot (have HYPE), now close by selling
                spot_px = _quantize(spot_bid * 0.9995, self._spot_px_decimals)
                orders.append(OrderSpec(self._spot_coin, False, size, spot_px, tif, reduce_only=False))  # Spot doesn't use reduce_only
            else:
                # We sold spot (short HYPE), now close by buying
                spot_px = _quantize_up(spot_ask * 1.0005, self._spot_px_decimals)
                orders.append(OrderSpec(self._spot_coin, True, size, spot_px, tif, reduce_only=False))

        # Execute close order
        if self._session is not None:
            try:
                payload, _ = self._build_action(orders)
                request = {"type": "action", "payload": payload}
                result = await self._session.post(request, timeout=10.0)

                response = result.get("response") or {}
                ok = isinstance(response, dict) and response.get("type") != "error"

                print(f"   Close result: {'✅ SUCCESS' if ok else '❌ FAILED'} - {response}")

                return {
                    "ok": ok,
                    "result": result,
                    "order": orders[0] if orders else None
                }
            except Exception as e:
                print(f"   ❌ Exception: {e}")
                return {"ok": False, "error": str(e)}

        return {"ok": False, "error": "No session"}

    async def close_hedge_immediately(
        self,
        direction: str,
        size: float,
        perp_bid: float,
        perp_ask: float,
        spot_bid: float,
        spot_ask: float,
    ) -> Dict[str, Any]:
        """
        Close an unhedged position immediately using IOC orders.
        Used when one leg of arbitrage fails - we need to close the other leg fast!

        Args:
            direction: Original trade direction ("perp->spot" or "spot->perp")
            size: Size of the position to close
            perp_bid, perp_ask, spot_bid, spot_ask: Current market prices
        """
        print(f"⚠️ CLOSING UNHEDGED POSITION: {direction}, size: {size}")

        # Build close orders (opposite of opening direction)
        tif = "Ioc"
        orders: List[OrderSpec] = []

        if direction == "perp->spot":
            # Original opened: perp SHORT + spot BUY
            # Close: perp LONG + spot SELL
            perp_px = _quantize_up(perp_ask * 1.0005, self._perp_px_decimals)  # Buy above ask
            spot_px = _quantize(spot_bid * 0.9995, self._spot_px_decimals)  # Sell below bid
            orders.append(OrderSpec(self._perp_name, True, size, perp_px, tif, reduce_only=True))   # BUY to close SHORT
            orders.append(OrderSpec(self._spot_coin, False, size, spot_px, tif, reduce_only=True))  # SELL to close BUY
        else:
            # Original opened: perp LONG + spot SELL
            # Close: perp SHORT + spot BUY
            perp_px = _quantize(perp_bid * 0.9995, self._perp_px_decimals)  # Sell below bid
            spot_px = _quantize_up(spot_ask * 1.0005, self._spot_px_decimals)  # Buy above ask
            orders.append(OrderSpec(self._perp_name, False, size, perp_px, tif, reduce_only=True))  # SELL to close LONG
            orders.append(OrderSpec(self._spot_coin, True, size, spot_px, tif, reduce_only=True))   # BUY to close SELL

        # Execute close orders
        if self._session is not None:
            try:
                perp_payload, _ = self._build_action([orders[0]])
                spot_payload, _ = self._build_action([orders[1]])

                perp_request = {"type": "action", "payload": perp_payload}
                spot_request = {"type": "action", "payload": spot_payload}

                perp_result = await self._session.post(perp_request, timeout=10.0)
                spot_result = await self._session.post(spot_request, timeout=10.0)

                perp_ok = isinstance(perp_result.get("response"), dict) and perp_result["response"].get("type") != "error"
                spot_ok = isinstance(spot_result.get("response"), dict) and spot_result["response"].get("type") != "error"

                if perp_ok and spot_ok:
                    print(f"✅ Hedge closed successfully!")
                    return {"ok": True, "perp": perp_result, "spot": spot_result}
                else:
                    print(f"⚠️ Hedge close had errors: perp_ok={perp_ok}, spot_ok={spot_ok}")
                    return {"ok": False, "perp": perp_result, "spot": spot_result}

            except Exception as e:
                print(f"❌ Error closing hedge: {e}")
                return {"ok": False, "error": str(e)}

        # Fallback to HTTP if no WebSocket
        print("⚠️ No WebSocket session, using HTTP fallback")
        ex = Exchange(self._wallet, base_url=self._base_url, meta=None, spot_meta=None)
        order_type: HLOrderType = {"limit": {"tif": "Ioc"}}

        try:
            perp_result = ex.order(self._perp_name, orders[0].is_buy, size, orders[0].limit_px, order_type, False)
            spot_result = ex.order(self._spot_symbol, orders[1].is_buy, size, orders[1].limit_px, order_type, False)

            return {"ok": True, "perp": perp_result, "spot": spot_result}
        except Exception as e:
            print(f"❌ HTTP fallback error: {e}")
            return {"ok": False, "error": str(e)}


