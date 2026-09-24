"""Global push-to-talk (Plan §7.2). The key owns the turn, not silence detection."""

import asyncio
import time

from pynput import keyboard


def resolve(name):
    """'f9' -> pynput key. Single characters are matched as KeyCode."""
    name = (name or "f9").strip().lower()
    special = getattr(keyboard.Key, name, None)
    if special is not None:
        return special
    return keyboard.KeyCode.from_char(name[:1])


class Hotkey:
    """Runs pynput's listener thread and hands presses to the event loop."""

    def __init__(self, loop, on_press, on_release, key="f9", min_press_ms=150, on_kill=None):
        self.loop = loop
        self.on_press = on_press
        self.on_release = on_release
        self.on_kill = on_kill
        self.key = resolve(key)
        self.min_press_ms = min_press_ms
        self.pressed_at = None
        self.escape_at = None
        self.listener = None

    def start(self):
        self.listener = keyboard.Listener(on_press=self._press, on_release=self._release)
        self.listener.daemon = True
        self.listener.start()
        return self.listener

    def stop(self):
        if self.listener:
            self.listener.stop()
            self.listener = None

    def _dispatch(self, coroutine_function, *args):
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(coroutine_function(*args)))

    def _press(self, key):
        if key == keyboard.Key.esc:
            # Kill switch: Esc held for a second (Plan §11.6).
            self.escape_at = self.escape_at or time.monotonic()
            if self.on_kill and time.monotonic() - self.escape_at >= 1.0:
                self.escape_at = None
                self._dispatch(self.on_kill)
            return
        if key != self.key or self.pressed_at is not None:
            return
        self.pressed_at = time.monotonic()
        self._dispatch(self.on_press)

    def _release(self, key):
        if key == keyboard.Key.esc:
            self.escape_at = None
            return
        if key != self.key or self.pressed_at is None:
            return
        held_ms = round((time.monotonic() - self.pressed_at) * 1000)
        self.pressed_at = None
        # Ignore accidental taps rather than sending a half-word to the STT.
        self._dispatch(self.on_release, held_ms >= self.min_press_ms, held_ms)
