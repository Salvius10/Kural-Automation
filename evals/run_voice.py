"""Replay recorded commands through the STT adapter (Plan §13.2).

    uv run python evals/run_voice.py                     current config.yaml settings
    uv run python evals/run_voice.py --no-format         format_turns off
    uv run python evals/run_voice.py --no-keyterms       keyterms off
    uv run python evals/run_voice.py --model universal-3-5-pro

Audio is streamed at real-time speed so the numbers mean something. Use it for
the settings bake-off and as a normalizer regression set.
"""

import argparse
import asyncio
import statistics
import sys
import time
import wave
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_env, settings  # noqa: E402
from voice.normalizer import normalize  # noqa: E402
from voice.stt_assemblyai import AssemblyAIStreaming  # noqa: E402

HERE = Path(__file__).with_name("voice")


def load_expected():
    path = HERE / "expected.yaml"
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def frames(path, frame_ms=60):
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != 16000 or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(f"{path.name}: expected 16 kHz mono 16-bit PCM")
        size = int(16000 * frame_ms / 1000)
        while True:
            chunk = handle.readframes(size)
            if not chunk:
                return
            yield chunk


async def transcribe(stt, path, frame_ms, realtime=True):
    await stt.begin_utterance()
    for chunk in frames(path, frame_ms):
        await stt.send_audio(chunk)
        if realtime:
            await asyncio.sleep(frame_ms / 1000)
    started = time.perf_counter()
    text = await stt.end_utterance()
    return text, round((time.perf_counter() - started) * 1000)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-format", action="store_true", help="format_turns=false")
    parser.add_argument("--no-keyterms", action="store_true", help="send no keyterms_prompt")
    parser.add_argument("--model", help="override speech_model")
    parser.add_argument("--fast", action="store_true", help="stream as fast as possible (latency is then meaningless)")
    arguments = parser.parse_args()

    load_env()
    config = settings()
    if arguments.no_format:
        config.stt.format_turns = False
    if arguments.no_keyterms:
        config.stt.keyterms_from_gazetteer = 0
    if arguments.model:
        config.stt.speech_model = arguments.model

    expected = load_expected()
    if not expected:
        print(f"No recordings. Add WAVs and expected.yaml under {HERE}/")
        return 1

    stt = AssemblyAIStreaming(config=config.stt)
    await stt.start()
    print(f"model={config.stt.speech_model} format_turns={config.stt.format_turns} "
          f"keyterms={config.stt.keyterms_from_gazetteer}\n")

    latencies, entity_hits, entity_total, files = [], 0, 0, 0
    for case in expected:
        path = HERE / case["file"]
        if not path.exists():
            print(f"  missing {case['file']}")
            continue
        files += 1
        raw, stt_ms = await transcribe(stt, path, config.audio.frame_ms, realtime=not arguments.fast)
        text, facts = normalize(raw)
        latencies.append(stt_ms)
        wanted = case.get("expected_goal_contains", [])
        hits = [w for w in wanted if w.lower() in text.lower()]
        entity_hits += len(hits)
        entity_total += len(wanted)
        missed = [w for w in wanted if w not in hits]
        print(f"  {case['file']:<12} {stt_ms:>5}ms  \"{text}\"" + (f"   MISSED {missed}" if missed else ""))
        del facts
    await stt.close()

    if not files:
        return 1
    accuracy = entity_hits / entity_total * 100 if entity_total else 100.0
    print(f"\n{files} file(s)")
    print(f"entity accuracy: {accuracy:.1f}%  ({entity_hits}/{entity_total})   target >= 95%")
    print(f"stt p50 {round(statistics.median(latencies))}ms  "
          f"p95 {sorted(latencies)[min(int(0.95 * (len(latencies) - 1)), len(latencies) - 1)]}ms   target p50 <= 300ms")
    return 0 if accuracy >= 95 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
