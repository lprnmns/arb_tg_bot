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


@dataclass
class ExecutedLeg:
    order: OrderSpec
    filled_size: float


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

        self._effective_leverage = settings.leverage

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
            self._effective_leverage = settings.leverage
        except Exception as e:
            print(f"⚠️  Failed to set leverage: {e}")
            print(f"   Continuing with configured leverage={settings.leverage}x (assumed already set on exchange)")
            self._effective_leverage = settings.leverage

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

    @property
    def effective_leverage(self) -> float:
        return max(self._effective_leverage, 1.0)

    def _build_order_specs(
        self,
        direction: str,
        use_ioc: bool,
        perp_bid: float,
        perp_ask: float,
        spot_bid: float,
        spot_ask: float,
        alloc_usd: float,
        size_override: Optional[Dict[str, float]] = None,
        reduce_only: bool = False,
    ) -> List[OrderSpec]:
        tif = "Ioc" if use_ioc else "Alo"
        orders: List[OrderSpec] = []

        use_override = size_override is not None and "perp" in size_override and "spot" in size_override

        target_notional = max(alloc_usd, settings.min_order_notional_usd)
        perp_notional = target_notional
        spot_notional = target_notional

        # Derive sizes from mid prices to avoid divide-by-zero.
        perp_ref = self._last_perp_mid or (perp_bid + perp_ask) / 2
        spot_ref = self._last_spot_mid or (spot_bid + spot_ask) / 2
        if perp_ref <= 0 or spot_ref <= 0:
            raise RuntimeError("Invalid reference price for sizing")

        if use_override:
            perp_size = _quantize_up(size_override["perp"], self._perp_sz_decimals)
            spot_size = _quantize_up(size_override["spot"], self._spot_sz_decimals)
        else:
            perp_size = _quantize_up(perp_notional / perp_ref, self._perp_sz_decimals)
            spot_size = _quantize_up(spot_notional / spot_ref, self._spot_sz_decimals)

        if perp_size <= 0 or spot_size <= 0:
            raise RuntimeError("Calculated trade size is zero")

        if use_override:
            print(f"📐 Size override: perp={perp_size} {self._perp_name}, spot={spot_size} {self._perp_name}")
        else:
            print(f"📐 Size calculation: alloc=${target_notional:.2f}")
            print(f"   Perp size: {perp_size} {self._perp_name}")
            print(f"   Spot size: {spot_size} {self._perp_name}")

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

    @staticmethod
    def _parse_order_response(
        orders: Sequence[OrderSpec],
        response: Optional[Dict[str, Any]],
    ) -> Tuple[List[ExecutedLeg], bool, List[str]]:
        """
        Extract filled sizes from a Hyperliquid order response.

        Returns (executed_legs, fully_filled, had_error)
        """
        if not orders:
            return [], True, []

        executed: List[ExecutedLeg] = []
        fully_filled = True
        errors: List[str] = []

        if not response:
            return executed, False, ["empty response"]

        data = None
        if isinstance(response, dict):
            if "data" in response:
                data = response.get("data")
            elif isinstance(response.get("response"), dict):
                data = response["response"].get("data")
            top_err = response.get("error")
            if top_err:
                errors.append(str(top_err))
        statuses = data.get("statuses") if isinstance(data, dict) else None
        statuses = statuses if isinstance(statuses, list) else []

        for idx, order in enumerate(orders):
            status: Any = statuses[idx] if idx < len(statuses) else None
            filled_sz = 0.0

            if isinstance(status, dict):
                status_flag = status.get("status")
                status_err = status.get("error")
                if status_err:
                    errors.append(str(status_err))
                elif isinstance(status_flag, str) and status_flag.lower() == "rejected":
                    errors.append(status_flag)
                filled_info = status.get("filled")
                if isinstance(filled_info, dict):
                    try:
                        filled_sz = float(filled_info.get("totalSz", 0) or 0)
                    except (TypeError, ValueError):
                        filled_sz = 0.0
                elif status_flag:
                    errors.append(str(status_flag))
            elif status == "error":
                errors.append("error")
            elif status is None:
                errors.append("missing status")

            if filled_sz > 0:
                executed.append(ExecutedLeg(order=order, filled_size=filled_sz))

            if filled_sz + 1e-9 < order.size:
                fully_filled = False

        if len(statuses) < len(orders):
            fully_filled = False
            errors.append("missing statuses")

        return executed, fully_filled, errors

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
        alloc_usd: Optional[float] = None,
        size_override: Optional[Dict[str, float]] = None,
        reduce_only: bool = False,  # ✅ FIX: Prevent opening wrong positions when closing
    ) -> Dict[str, Any]:
        self.update_mid_prices(perp_bid, perp_ask, spot_bid, spot_ask)
        notional = alloc_usd if alloc_usd is not None else settings.alloc_per_trade_usd
        orders = self._build_order_specs(
            direction,
            use_ioc,
            perp_bid,
            perp_ask,
            spot_bid,
            spot_ask,
            notional,
            size_override=size_override,
            reduce_only=reduce_only,
        )
        all_perp_specs = [o for o in orders if o.coin == self._perp_name]
        all_spot_specs = [o for o in orders if o.coin != self._perp_name]
        ws_error = None
        executed_legs: List[ExecutedLeg] = []
        perp_result = None
        spot_result = None
        if self._session is not None:
            try:
                perp_specs = list(all_perp_specs)
                spot_specs = list(all_spot_specs)

                perp_response: Dict[str, Any] = {}
                spot_response: Dict[str, Any] = {}
                perp_full = True
                spot_full = True
                perp_errors: List[str] = []
                spot_errors: List[str] = []
                perp_attempted = False
                spot_attempted = False

                if direction == "perp->spot":
                    if perp_specs:
                        perp_attempted = True
                        perp_payload, _ = self._build_action(perp_specs)
                        perp_request = {"type": "action", "payload": perp_payload}
                        perp_result = await self._session.post(perp_request, timeout=10.0)
                        perp_response = perp_result.get("response") or {}
                        perp_exec, perp_full, perp_errors = self._parse_order_response(perp_specs, perp_response)
                        executed_legs.extend(perp_exec)
                        if not (perp_full and not perp_errors):
                            print("❌ Perp leg failed or partial; skipping spot leg to avoid unhedged position")
                    if spot_specs and (perp_full and not perp_error):
                        spot_attempted = True
                        spot_payload, _ = self._build_action(spot_specs)
                        spot_request = {"type": "action", "payload": spot_payload}
                        spot_result = await self._session.post(spot_request, timeout=10.0)
                        spot_response = spot_result.get("response") or {}
                        spot_exec, spot_full, spot_errors = self._parse_order_response(spot_specs, spot_response)
                        executed_legs.extend(spot_exec)
                else:
                    if spot_specs:
                        spot_attempted = True
                        spot_payload, _ = self._build_action(spot_specs)
                        spot_request = {"type": "action", "payload": spot_payload}
                        spot_result = await self._session.post(spot_request, timeout=10.0)
                        spot_response = spot_result.get("response") or {}
                        spot_exec, spot_full, spot_errors = self._parse_order_response(spot_specs, spot_response)
                        executed_legs.extend(spot_exec)
                        if not (spot_full and not spot_errors):
                            print("❌ Spot leg failed or partial; skipping perp leg to avoid unhedged position")
                    if perp_specs and (spot_full and not spot_error):
                        perp_attempted = True
                        perp_payload, _ = self._build_action(perp_specs)
                        perp_request = {"type": "action", "payload": perp_payload}
                        perp_result = await self._session.post(perp_request, timeout=10.0)
                        perp_response = perp_result.get("response") or {}
                        perp_exec, perp_full, perp_errors = self._parse_order_response(perp_specs, perp_response)
                        executed_legs.extend(perp_exec)

                perp_ok = (not perp_attempted) or (perp_full and not perp_errors)
                spot_ok = (not spot_attempted) or (spot_full and not spot_errors)
                ok = perp_ok and spot_ok

                if perp_attempted:
                    print(f"   PERP response: {'✅ OK' if perp_ok else '❌ FAILED'} - {perp_response}")
                    if perp_errors:
                        print(f"     PERP errors: {', '.join(perp_errors)}")
                if spot_attempted:
                    print(f"   SPOT response: {'✅ OK' if spot_ok else '❌ FAILED'} - {spot_response}")
                    if spot_errors:
                        print(f"     SPOT errors: {', '.join(spot_errors)}")

                if not ok and executed_legs:
                    print("\n⚠️  Trade legs not fully matched. Flattening executed exposure...")
                    combined_errors = perp_errors + spot_errors
                    if combined_errors:
                        print(f"   Reported errors: {', '.join(combined_errors)}")
                    for leg in executed_legs:
                        try:
                            close_result = await self.close_single_leg(
                                is_perp=(leg.order.coin == self._perp_name),
                                is_buy=leg.order.is_buy,
                                size=leg.filled_size,
                                perp_bid=perp_bid,
                                perp_ask=perp_ask,
                                spot_bid=spot_bid,
                                spot_ask=spot_ask,
                            )
                            if close_result.get("ok"):
                                print(f"   ✅ Flattened {leg.filled_size} {leg.order.coin}")
                            else:
                                print(f"   ❌ Failed to flatten {leg.order.coin}: {close_result}")
                        except Exception as close_exc:
                            print(f"   ❌ Exception flattening {leg.order.coin}: {close_exc}")
                            import traceback
                            traceback.print_exc()

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
                    recorded_orders = executed_legs if executed_legs else [ExecutedLeg(order=o, filled_size=o.size) for o in orders]
                    for leg in recorded_orders:
                        o = leg.order
                        order_requests.append({
                            "coin": o.coin,
                            "is_buy": o.is_buy,
                            "sz": leg.filled_size,
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
                        "errors": {"perp": perp_errors, "spot": spot_errors},
                        "request_id": str(perp_result.get("id")) if perp_result and perp_result.get("id") is not None else None,
                    }
            except Exception as e:
                ws_error = repr(e)

        ex = Exchange(self._wallet, base_url=self._base_url, meta=None, spot_meta=None)
        order_type: HLOrderType = {"limit": {"tif": "Ioc" if use_ioc else "Alo"}}
        http_resp: Dict[str, Any] = {}
        http_deadman = None
        http_executed: List[ExecutedLeg] = []

        def _http_payload(spec: OrderSpec) -> Dict[str, Any]:
            coin = self._spot_symbol if spec.coin == self._spot_coin else spec.coin
            return {
                "coin": coin,
                "is_buy": spec.is_buy,
                "sz": spec.size,
                "limit_px": spec.limit_px,
                "order_type": order_type,
                "reduce_only": False,
            }

        def _parse_http(specs: Sequence[OrderSpec], resp: Dict[str, Any]) -> Tuple[bool, List[str]]:
            body = resp.get("response") if isinstance(resp, dict) else None
            execs, full, errs = self._parse_order_response(specs, body if isinstance(body, dict) else None)
            http_executed.extend(execs)
            return full, errs

        perp_specs = list(all_perp_specs)
        spot_specs = list(all_spot_specs)
        perp_full = True
        spot_full = True
        perp_errors: List[str] = []
        spot_errors: List[str] = []

        if direction == "perp->spot":
            if perp_specs:
                payload = [_http_payload(spec) for spec in perp_specs]
                resp = ex.bulk_orders(payload)
                http_resp["perp"] = resp
                perp_full, errs = _parse_http(perp_specs, resp)
                perp_errors.extend(errs)
                if not perp_full or errs:
                    spot_specs = []
            if spot_specs:
                payload = [_http_payload(spec) for spec in spot_specs]
                resp = ex.bulk_orders(payload)
                http_resp["spot"] = resp
                spot_full, errs = _parse_http(spot_specs, resp)
                spot_errors.extend(errs)
        else:
            if spot_specs:
                payload = [_http_payload(spec) for spec in spot_specs]
                resp = ex.bulk_orders(payload)
                http_resp["spot"] = resp
                spot_full, errs = _parse_http(spot_specs, resp)
                spot_errors.extend(errs)
                if not spot_full or errs:
                    perp_specs = []
            if perp_specs:
                payload = [_http_payload(spec) for spec in perp_specs]
                resp = ex.bulk_orders(payload)
                http_resp["perp"] = resp
                perp_full, errs = _parse_http(perp_specs, resp)
                perp_errors.extend(errs)

        perp_ok = (not perp_specs) or (perp_full and not perp_errors)
        spot_ok = (not spot_specs) or (spot_full and not spot_errors)
        http_ok = perp_ok and spot_ok

        if not http_ok and http_executed:
            print("\n⚠️  HTTP fallback resulted in partial execution. Flattening...")
            combined_errors = perp_errors + spot_errors
            if combined_errors:
                print(f"   Reported errors: {', '.join(combined_errors)}")
            for leg in http_executed:
                try:
                    close_result = await self.close_single_leg(
                        is_perp=(leg.order.coin == self._perp_name),
                        is_buy=leg.order.is_buy,
                        size=leg.filled_size,
                        perp_bid=perp_bid,
                        perp_ask=perp_ask,
                        spot_bid=spot_bid,
                        spot_ask=spot_ask,
                    )
                    if close_result.get("ok"):
                        print(f"   ✅ Flattened {leg.filled_size} {leg.order.coin}")
                    else:
                        print(f"   ❌ Failed to flatten {leg.order.coin}: {close_result}")
                except Exception as close_exc:
                    print(f"   ❌ Exception flattening {leg.order.coin}: {close_exc}")
                    import traceback
                    traceback.print_exc()

        if not use_ioc and deadman_ms > 0:
            try:
                http_deadman = ex.schedule_cancel(get_timestamp_ms() + deadman_ms)
            except Exception as schedule_exc:
                http_deadman = {"error": repr(schedule_exc)}

        recorded_orders = http_executed if http_executed else [ExecutedLeg(order=spec, filled_size=spec.size) for spec in orders]
        http_orders: List[Dict[str, Any]] = []
        for leg in recorded_orders:
            spec = leg.order
            coin = self._spot_symbol if spec.coin == self._spot_coin else spec.coin
            http_orders.append({
                "coin": coin,
                "is_buy": spec.is_buy,
                "sz": leg.filled_size,
                "limit_px": spec.limit_px,
                "order_type": order_type,
                "reduce_only": False,
            })

        return {
            "ok": http_ok,
            "mm_best_bps": mm_best_bps,
            "request": {"direction": direction, "use_ioc": use_ioc, "orders": http_orders},
            "response": {"order": http_resp, "scheduleCancel": http_deadman, "ws_error": ws_error},
            "errors": {"perp": perp_errors, "spot": spot_errors},
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
                execs, full, errs = self._parse_order_response(orders, response)
                ok = bool(execs) and not errs

                if errs:
                    print(f"   Close errors: {', '.join(errs)}")
                print(f"   Close result: {'✅ SUCCESS' if ok else '❌ FAILED'} - {response}")

                return {
                    "ok": ok,
                    "result": result,
                    "order": orders[0] if orders else None,
                    "errors": errs,
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
