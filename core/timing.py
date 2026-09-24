"""Per-stage timestamps. One reference point: the latest key release (Plan §4.1)."""

import time


class Clock:
    """`t_ms` on every event is measured from `release()`; stages are named spans."""

    def __init__(self):
        self.origin = time.perf_counter()
        self.stages: dict[str, int] = {}
        self._open: dict[str, float] = {}

    def release(self):
        """Key release: the zero point of a turn's latency budget."""
        self.origin = time.perf_counter()
        self.stages = {}
        self._open = {}
        return self.origin

    def t_ms(self):
        return round((time.perf_counter() - self.origin) * 1000)

    def start(self, stage):
        self._open[stage] = time.perf_counter()

    def stop(self, stage):
        started = self._open.pop(stage, None)
        elapsed = 0 if started is None else round((time.perf_counter() - started) * 1000)
        self.stages[stage] = elapsed
        return elapsed

    def span(self, stage):
        return _Span(self, stage)

    def report(self):
        return dict(self.stages)


class _Span:
    def __init__(self, clock, stage):
        self.clock, self.stage, self.ms = clock, stage, 0

    def __enter__(self):
        self.clock.start(self.stage)
        return self

    def __exit__(self, *_args):
        self.ms = self.clock.stop(self.stage)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *args):
        return self.__exit__(*args)
