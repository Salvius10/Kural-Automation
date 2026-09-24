"""Local status page: one HTML file, one WebSocket, no framework (Plan §7.10)."""

import asyncio
import socket
from pathlib import Path

import orjson
import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

INDEX = Path(__file__).with_name("index.html")


def build(bus, session=None):
    async def index(request):
        return FileResponse(INDEX, headers={"Cache-Control": "no-store"})

    async def status(request):
        return JSONResponse(session.status() if session else {"state": "IDLE"})

    async def stream(websocket):
        """Pushes events until the page goes away.

        The disconnect only surfaces on a receive, so one runs alongside the
        queue; without it the handler would block shutdown forever.
        """
        await websocket.accept()
        queue = bus.subscribe()
        closed = asyncio.create_task(websocket.receive())
        try:
            if session:
                await websocket.send_text(orjson.dumps({"type": "Status", **session.status()}).decode())
            while True:
                nxt = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({nxt, closed}, return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    nxt.cancel()
                    return
                await websocket.send_text(orjson.dumps(nxt.result().model_dump()).decode())
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            closed.cancel()
            bus.unsubscribe(queue)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/status", status),
            WebSocketRoute("/events", stream),
        ]
    )


def bind(port, host="127.0.0.1"):
    """Claim the port ourselves: uvicorn calls sys.exit() if the bind fails."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((host, port))
    except OSError:
        listener.close()
        raise
    listener.listen(64)
    listener.setblocking(False)
    return listener


def serve(bus, session=None, port=8766):
    """A uvicorn server on loopback, run as a task on the agent's own loop.

    Returns `(server, sockets)`; pass the sockets to `server.serve(sockets=...)`
    so a port already in use surfaces here instead of killing the process.
    """
    listener = bind(port)
    config = uvicorn.Config(
        build(bus, session),
        log_level="warning",
        access_log=False,
        loop="none",
    )
    return uvicorn.Server(config), [listener]
