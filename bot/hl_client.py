import asyncio, json, time
import httpx, websockets
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

from .config import settings
from .execution import WsPostSession
async def info_post(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(settings.hl_info_url, json=payload)
        r.raise_for_status()
        return r.json()
async def resolve_spot_index(base: str, quote: str="USDC") -> Optional[int]:
    data = await info_post({"type":"spotMeta"})
    tokens = {t["index"]: t["name"] for t in data.get("tokens",[])}
    usdc_idx = None
    for idx, nm in tokens.items():
        if isinstance(nm,str) and nm.upper()==quote.upper():
            usdc_idx = idx; break
    if usdc_idx is None: return None
    for p in data.get("universe",[]):
        if usdc_idx in p.get("tokens",[]):
            other = p["tokens"][0] if p["tokens"][1]==usdc_idx else p["tokens"][1]
            other_name = tokens.get(other,"")
            candidate = other_name[1:] if other_name.startswith("U") else other_name
            if candidate.upper()==base.upper():
                return p["index"]
    return None
def best_bid_ask(l2) -> Tuple[Optional[float],Optional[float]]:
    ll = l2.get("levels") if isinstance(l2,dict) else l2
    if not isinstance(ll,list) or len(ll)!=2: return None,None
    bids = ll[0] if isinstance(ll[0],list) else []
    asks = ll[1] if isinstance(ll[1],list) else []
    bid = float(bids[0]["px"]) if bids else None
    ask = float(asks[0]["px"]) if asks else None
    return bid, ask
def bps(x: float) -> float: return x*1e4
def compute_edges(perp_bid, perp_ask, spot_bid, spot_ask, fees) -> Dict[str,float]:
    mid_ps = (perp_bid + spot_ask) / 2.0
    mid_sp = (spot_bid + perp_ask) / 2.0
    mid_ref = (mid_ps + mid_sp) / 2.0
    fee_mm = fees["perp"]["maker"] + fees["spot"]["maker"]
    fee_tt = fees["perp"]["taker"] + fees["spot"]["taker"]
    e_ps_raw = bps((perp_bid - spot_ask) / mid_ps) if mid_ps else 0.0
    e_sp_raw = bps((spot_bid - perp_ask) / mid_sp) if mid_sp else 0.0
    return {
        "ps_mm": e_ps_raw - fee_mm,
        "sp_mm": e_sp_raw - fee_mm,
        "ps_tt": e_ps_raw - fee_tt,
        "sp_tt": e_sp_raw - fee_tt,
        "mid_ref": mid_ref,
    }
async def ws_loop(spot_index: int, strategy):
    sub_perp = {"method":"subscribe","subscription":{"type":"l2Book","coin": settings.pair_base}}
    sub_spot = {"method":"subscribe","subscription":{"type":"l2Book","coin": f"@{spot_index}"}}
    print(f"📡 Connecting to WebSocket: {settings.hl_ws_url}")
    async for ws in websockets.connect(settings.hl_ws_url, ping_interval=15, ping_timeout=15):
        print("✅ WebSocket connected!")
        session = WsPostSession(ws)
        strategy.attach_post_session(session)
        try:
            print(f"📤 Subscribing to {settings.pair_base} (perp) and @{spot_index} (spot)...")
            await ws.send(json.dumps(sub_perp))
            await ws.send(json.dumps(sub_spot))
            print("✅ Subscribed to market data feeds")
            last_perp = last_spot = None
            while True:
                t0 = time.perf_counter_ns()
                msg = await ws.recv()
                t1 = time.perf_counter_ns()
                data = json.loads(msg)
                if isinstance(data, dict):
                    if data.get("channel") == "post":
                        session.handle_post_response(data.get("data", {}))
                        continue
                if isinstance(data,dict) and data.get("channel")=="l2Book":
                    coin = data["data"].get("coin")
                    levels = data["data"]
                    if coin == settings.pair_base:
                        last_perp = levels
                    elif coin == f"@{spot_index}":
                        last_spot = levels
                    if last_perp and last_spot:
                        pbid,pask = best_bid_ask(last_perp)
                        sbid,sask = best_bid_ask(last_spot)
                        if None not in (pbid,pask,sbid,sask):
                            recv_ms = int((t1 - t0)/1e6)
                            await strategy.on_edge(pbid,pask,sbid,sask,recv_ms)
        except Exception as exc:
            print(f"❌ WebSocket error: {exc}")
            import traceback
            traceback.print_exc()
            session.close(exc)
            await asyncio.sleep(1.0)
            strategy.attach_post_session(None)
            print("🔄 Reconnecting to WebSocket...")
            continue
        finally:
            session.close()
            strategy.attach_post_session(None)
