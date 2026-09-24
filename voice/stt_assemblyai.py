"""AssemblyAI Universal Streaming v3 (Plan §7.3).

Push-to-talk owns the turn: the model's own endpointing is turned down so a
mid-sentence pause cannot cut the user off, and key release sends
`ForceEndpoint` instead of waiting for silence.

Verified against the v3 streaming reference (2026-09): `wss://streaming.assemblyai.com/v3/ws`,
API key in the `Authorization` header with no "Bearer" prefix, `pcm_s16le` at
16 kHz, `keyterms_prompt` as a JSON array (max 100 terms of <= 50 chars),
client messages `ForceEndpoint` / `Terminate` / `KeepAlive` / `UpdateConfiguration`,
and `Turn` messages carrying `transcript`, `end_of_turn` and `turn_is_formatted`.
Sessions auto-close after three hours, so the reader reconnects.
"""

import asyncio
import json
import os
import time
from urllib.parse import urlencode

import websockets

from core.config import settings
from voice.normalizer import keyterms

FORMATTED_GRACE_S = 0.35  # how long to wait for the punctuated version of a final turn
KEEPALIVE_S = 20


class AssemblyAIStreaming:
    """Implements the `SpeechToText` seam. One socket, kept warm across utterances."""

    def __init__(self, bus=None, api_key=None, config=None):
        self.bus = bus
        self.config = config or settings().stt
        self.api_key = api_key or os.environ.get("ASSEMBLYAI_API_KEY", "")
        self.socket = None
        self.reader: asyncio.Task | None = None
        self.keeper: asyncio.Task | None = None
        self.partial_queue: asyncio.Queue = asyncio.Queue()
        self.final: asyncio.Future | None = None
        self.formatted: asyncio.Future | None = None
        self.closing = False
        self.last_sent = 0.0

    # ------------------------------------------------------------ connection

    def url(self):
        parameters = {
            "sample_rate": self.config.sample_rate,
            "encoding": self.config.encoding,
            "speech_model": self.config.speech_model,
            "format_turns": str(self.config.format_turns).lower(),
            # Push-to-talk decides when the turn ends, so make the model's own
            # endpointing effectively unreachable while the key is held.
            "end_of_turn_confidence_threshold": self.config.end_of_turn_confidence_threshold,
            "max_turn_silence": self.config.max_turn_silence_ms,
        }
        terms = keyterms(self.config.keyterms_from_gazetteer)[:100]
        if terms:
            parameters["keyterms_prompt"] = json.dumps([t[:50] for t in terms])
        return f"{self.config.url}?{urlencode(parameters)}"

    async def start(self):
        if not self.api_key:
            raise RuntimeError("ASSEMBLYAI_API_KEY is not set")
        await self.connect()
        return self

    async def connect(self):
        self.socket = await websockets.connect(
            self.url(),
            additional_headers={"Authorization": self.api_key},
            max_queue=64,
            ping_interval=20,
        )
        self.closing = False
        self.reader = asyncio.create_task(self.read())
        self.keeper = asyncio.create_task(self.keepalive())
        return self.socket

    async def reconnect(self):
        """Sessions expire and networks drop; a warm socket is the whole latency trick."""
        delay = 0.5
        while not self.closing:
            try:
                await self.connect()
                return True
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10)
        return False

    async def keepalive(self):
        try:
            while not self.closing:
                await asyncio.sleep(KEEPALIVE_S)
                if self.socket and time.monotonic() - self.last_sent > KEEPALIVE_S:
                    await self.send({"type": "KeepAlive"})
        except (asyncio.CancelledError, websockets.WebSocketException):
            return

    async def send(self, message):
        if not self.socket:
            return
        self.last_sent = time.monotonic()
        await self.socket.send(json.dumps(message) if isinstance(message, dict) else message)

    # --------------------------------------------------------------- reading

    async def read(self):
        try:
            async for raw in self.socket:
                if isinstance(raw, bytes):
                    continue
                message = json.loads(raw)
                if message.get("type") == "Turn":
                    self.on_turn(message)
        except asyncio.CancelledError:
            raise
        except websockets.WebSocketException:
            if not self.closing:
                asyncio.create_task(self.reconnect())

    def on_turn(self, message):
        text = (message.get("utterance") or message.get("transcript") or "").strip()
        if not message.get("end_of_turn"):
            if text:
                self.partial_queue.put_nowait(text)
            return
        if message.get("turn_is_formatted") and self.formatted and not self.formatted.done():
            self.formatted.set_result(text)
        if self.final and not self.final.done():
            self.final.set_result(text)

    # ------------------------------------------------------------- utterance

    async def begin_utterance(self):
        loop = asyncio.get_running_loop()
        self.final = loop.create_future()
        self.formatted = loop.create_future()
        while not self.partial_queue.empty():
            self.partial_queue.get_nowait()
        if self.socket is None or self.socket.close_code is not None:
            await self.reconnect()

    async def send_audio(self, pcm):
        if self.socket is None:
            return
        try:
            self.last_sent = time.monotonic()
            await self.socket.send(pcm)
        except websockets.WebSocketException:
            await self.reconnect()

    async def end_utterance(self, timeout=3.0):
        """Key release -> ForceEndpoint -> the final turn. Returns the transcript."""
        if self.final is None:
            return ""
        await self.send({"type": "ForceEndpoint"})
        try:
            text = await asyncio.wait_for(self.final, timeout=timeout)
        except (asyncio.TimeoutError, websockets.WebSocketException):
            return ""
        if self.config.format_turns and self.formatted and not self.formatted.done():
            # The punctuated version arrives a moment after the raw one.
            try:
                text = await asyncio.wait_for(self.formatted, timeout=FORMATTED_GRACE_S)
            except asyncio.TimeoutError:
                pass
        return text

    async def partials(self):
        while True:
            yield await self.partial_queue.get()

    async def close(self):
        self.closing = True
        for task in (self.reader, self.keeper):
            if task and not task.done():
                task.cancel()
        if self.socket:
            try:
                await self.send({"type": "Terminate"})
                await self.socket.close()
            except Exception:
                pass
            self.socket = None
