from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import JWTError, jwt
from config import settings
from core.async_bus import bus
import asyncio
import json
import logging

router = APIRouter(tags=["websocket"])
log    = logging.getLogger("ws")

def _verify_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

@router.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket, token: str = Query(...)):
    email = _verify_token(token)
    if not email:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    q = bus.subscribe("signals")
    log.info("WS connected: %s", email)

    try:
        while True:
            # Wait for a signal OR a ping from the client
            try:
                data = await asyncio.wait_for(q.get(), timeout=25)
                await websocket.send_text(json.dumps(data))
            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                await websocket.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        bus.unsubscribe("signals", q)
        log.info("WS disconnected: %s", email)
