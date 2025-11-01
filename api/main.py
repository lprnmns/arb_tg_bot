import asyncio
import contextlib
import os
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from bot.config import settings

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.get("/health")
async def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}
@app.get("/scanner")
async def scanner():
    dsn = f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@{os.getenv('POSTGRES_HOST')}:{os.getenv('POSTGRES_PORT')}/{os.getenv('POSTGRES_DB')}"
    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch("SELECT ts, base, spot_index, edge_ps_mm_bps, edge_sp_mm_bps, mid_ref, recv_ms, send_ms FROM edges ORDER BY ts DESC LIMIT 200")
    await conn.close()
    return [dict(r) for r in rows]
@app.on_event("startup")
async def startup():
    app.state.redis = aioredis.Redis(**settings.redis_kwargs, encoding="utf-8", decode_responses=True)
@app.on_event("shutdown")
async def shutdown():
    redis: aioredis.Redis = app.state.redis
    if redis:
        await redis.close()
@app.websocket("/ws/edges")
async def ws_edges(ws: WebSocket):
    await ws.accept()
    redis: aioredis.Redis = app.state.redis
    pubsub = redis.pubsub()
    channel = settings.edges_channel
    await pubsub.subscribe(channel)
    # Send an initial frame so the client sees activity immediately
    try:
        await ws.send_text('{"type":"init","channel":"%s"}' % channel)
    except Exception:
        pass
    async def consume_client():
        try:
            while True:
                msg = await ws.receive_text()
                # Handle ping-pong for RTT measurement
                if msg == "ping":
                    await ws.send_text(f"pong:{msg}")
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
    reader = asyncio.create_task(consume_client())
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            data = message["data"]
            if data is None:
                continue
            await ws.send_text(data)
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
        with contextlib.suppress(Exception):
            await reader
        await pubsub.unsubscribe(channel)
        await pubsub.close()

