"""Entry point: warm everything, then listen (Plan §3, §4.2.1).

    uv run python main.py           voice: hold the hotkey and speak
    uv run python main.py --type    typed goals on stdin, no microphone
"""

import argparse
import asyncio
import contextlib
import sys
import time

from agent.browser import AsyncBrowser
from core import http
from core.bus import Bus
from core.config import load_env, settings
from core.events import Error, KeyDown, KeyUp, Notice, TranscriptPartial
from core.log import JsonlLog
from core.mercury import Mercury
from core.session import Session
from ui.server import serve


def use_fast_event_loop():
    """uvloop where it exists; Windows gets the selector loop."""
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass


class VoiceInput:
    """Hotkey + microphone + STT, wired to one session."""

    def __init__(self, session, bus, loop):
        from voice.hotkey import Hotkey
        from voice.mic import Microphone
        from voice.stt_assemblyai import AssemblyAIStreaming

        config = settings()
        self.session = session
        self.bus = bus
        self.stt = AssemblyAIStreaming(bus)
        self.mic = Microphone(loop, config.audio.sample_rate, config.audio.frame_ms)
        self.hotkey = Hotkey(
            loop,
            self.press,
            self.release,
            key=config.hotkey,
            min_press_ms=config.min_press_ms,
            on_kill=self.kill,
        )
        self.pump: asyncio.Task | None = None
        self.partials: asyncio.Task | None = None

    async def start(self):
        await self.stt.start()
        self.mic.open()
        self.hotkey.start()
        self.partials = asyncio.create_task(self.show_partials())
        return self

    async def show_partials(self):
        with contextlib.suppress(asyncio.CancelledError):
            async for text in self.stt.partials():
                self.bus.publish(TranscriptPartial(text=text))

    async def press(self):
        self.bus.clock.release()  # provisional zero point; reset precisely on release
        self.bus.publish(KeyDown())
        self.session.key_down()
        # Capture first. Opening the socket can take ~1 s, and the first word is
        # spoken during it -- the frames wait in the mic queue (~4 s of buffer)
        # and the pump drains them in order once the socket is up.
        self.mic.begin()
        await self.stt.begin_utterance()
        self.pump = asyncio.create_task(self.stream_audio())

    async def stream_audio(self):
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                frame = await self.mic.queue.get()
                await self.stt.send_audio(frame)

    async def release(self, long_enough, held_ms):
        self.mic.end()
        if self.pump:
            self.pump.cancel()
            self.pump = None
        for frame in await self.mic.drain():
            await self.stt.send_audio(frame)
        # Everything after this point is measured against the key release.
        self.bus.clock.release()
        self.bus.publish(KeyUp(held_ms=held_ms))
        if not long_enough:
            return self.bus.publish(Notice(text="Too short — hold the key while you speak."))
        started = time.perf_counter()
        text = await self.stt.end_utterance()
        stt_ms = round((time.perf_counter() - started) * 1000)
        if not text:
            return self.bus.publish(Notice(text="Didn't catch that.", level="warn"))
        await self.session.utterance(text, stt_ms=stt_ms, provider="assemblyai")

    async def kill(self):
        await self.session.cancel("esc held")

    async def close(self):
        for task in (self.pump, self.partials):
            if task and not task.done():
                task.cancel()
        self.hotkey.stop()
        self.mic.close()
        await self.stt.close()


async def typed_input(session, bus):
    """Stdin stands in for the microphone (Plan M2)."""
    loop = asyncio.get_running_loop()
    print("Type a goal and press enter. Ctrl-C to quit.", flush=True)
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        bus.clock.release()
        try:
            await session.utterance(line, provider="typed")
        except Exception as error:
            bus.publish(Error(where="typed-input", message=str(error), recoverable=True))


async def run(typed=False):
    load_env()
    config = settings()
    bus = Bus()
    log = JsonlLog(bus.session_id)
    bus.add_sink(log)

    await http.warm()
    browser = AsyncBrowser(open_in=config.navigation.open_in, activate=config.navigation.activate)
    session = Session(bus, browser, text_writer=Mercury())

    try:
        server, sockets = serve(bus, session, config.status_port)
        server_task = asyncio.create_task(server.serve(sockets=sockets))
        print(f"Status page: http://127.0.0.1:{config.status_port}", flush=True)
    except OSError:
        server, server_task = None, None
        print(f"Port {config.status_port} is busy; running without the status page.", flush=True)
    print(f"Log: {log.path}", flush=True)

    await session.start()
    voice = None
    if not typed:
        try:
            voice = await VoiceInput(session, bus, asyncio.get_running_loop()).start()
            print(f"Hold {config.hotkey.upper()} and speak. Esc (1s) cancels.", flush=True)
        except Exception as error:
            print(f"Voice input unavailable ({error}); falling back to typed input.", flush=True)
            voice = None
    try:
        await typed_input(session, bus) if (typed or voice is None) else await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if voice:
            await voice.close()
        await session.close()
        if server:
            server.should_exit = True
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
        await http.close_all()
        log.close()


def main():
    parser = argparse.ArgumentParser(description="Kural — voice-enabled browser agent")
    parser.add_argument("--type", action="store_true", help="read goals from stdin instead of the microphone")
    arguments = parser.parse_args()
    use_fast_event_loop()
    try:
        asyncio.run(run(typed=arguments.type))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
