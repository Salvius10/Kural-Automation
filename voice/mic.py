"""16 kHz mono int16 capture, pushed to the loop in small frames (Plan §7.2)."""

import asyncio

import sounddevice


class Microphone:
    """Frames arrive on PortAudio's thread and cross to the loop with call_soon_threadsafe."""

    def __init__(self, loop, sample_rate=16000, frame_ms=60, queue_size=64):
        self.loop = loop
        self.sample_rate = sample_rate
        self.frames = int(sample_rate * frame_ms / 1000)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.stream = None
        self.capturing = False

    def _callback(self, indata, _frames, _time, _status):
        if not self.capturing:
            return
        pcm = bytes(indata)
        self.loop.call_soon_threadsafe(self._put, pcm)

    def _put(self, pcm):
        try:
            self.queue.put_nowait(pcm)
        except asyncio.QueueFull:
            pass  # Dropping a frame beats blocking the audio thread.

    def open(self):
        """Kept open for the whole session: opening a stream costs ~100 ms."""
        if self.stream is None:
            self.stream = sounddevice.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.frames,
                dtype="int16",
                channels=1,
                callback=self._callback,
            )
            self.stream.start()
        return self.stream

    def begin(self):
        while not self.queue.empty():
            self.queue.get_nowait()
        self.capturing = True

    def end(self):
        self.capturing = False

    async def drain(self):
        """Every frame captured so far, without waiting for another."""
        chunks = []
        while not self.queue.empty():
            chunks.append(self.queue.get_nowait())
        return chunks

    def close(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
