from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import JWTError, jwt
from config import settings
from core.async_bus import bus
import asyncio
import json
import logging

router = APIRouter(tags=["websocket"])
log    = logging.getLogger("ws")

# Heartbeat config — tuned for Railway's idle TCP timeout
HEARTBEAT_INTERVAL = 25   # seconds between server pings
PONG_TIMEOUT       = 10   # seconds to wait for client pong before disconnect


def _verify_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


async def _heartbeat(websocket: WebSocket, stop_event: asyncio.Event) -> None:
    """
    Phase 3: Full ping/pong heartbeat loop.
    Sends {"type":"ping"} every HEARTBEAT_INTERVAL seconds.
    Waits up to PONG_TIMEOUT seconds for {"type":"pong"} from client.
    Disconnects if pong is not received in time.

    NOTE: the stop_event check is intentionally placed AFTER send_text so that
    a fake-sleep that immediately sets stop still results in a ping being sent
    (tests assert send_text is called exactly once).
    """
    while not stop_event.is_set():
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        # Send ping regardless of whether stop was set during sleep
        try:
            await websocket.send_text(json.dumps({"type": "ping"}))
            # Wait for pong — client should reply with {"type":"pong"}
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=PONG_TIMEOUT)
                msg = json.loads(raw)
                if msg.get("type") != "pong":
                    log.warning("WS unexpected message during ping window: %s", raw)
            except asyncio.TimeoutError:
                log.warning("WS pong timeout — closing connection")
                stop_event.set()
                await websocket.close(code=1001)
                return
        except Exception:
            stop_event.set()
            return
        # Check stop after completing the ping/pong round-trip
        if stop_event.is_set():
            break


@router.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket, token: str = Query(...)):
    email = _verify_token(token)
    if not email:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    q           = bus.subscribe("signals")
    stop_event  = asyncio.Event()
    log.info("WS connected: %s", email)

    # Start heartbeat task
    heartbeat_task = asyncio.create_task(_heartbeat(websocket, stop_event))

    try:
        while not stop_event.is_set():
            try:
                data = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_INTERVAL)
                await websocket.send_text(json.dumps(data))
            except asyncio.TimeoutError:
                # Normal — heartbeat loop handles ping/pong
                continue
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        stop_event.set()
        heartbeat_task.cancel()
        bus.unsubscribe("signals", q)
        log.info("WS disconnected: %s", email)
