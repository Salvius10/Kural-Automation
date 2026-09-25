"""The microphone and STT adapter, offline (Plan §7.2, §7.3)."""

import asyncio

import pytest

from voice.mic import Microphone


class FakeConfig:
    url = "wss://streaming.assemblyai.com/v3/ws"
    speech_model = "universal-streaming-english"
    sample_rate = 16000
    encoding = "pcm_s16le"
    format_turns = True
    end_of_turn_confidence_threshold = 1.0
    max_turn_silence_ms = 2500
    keyterms_from_gazetteer = 5
    idle_close_s = 30.0


def adapter():
    from voice.stt_assemblyai import AssemblyAIStreaming

    return AssemblyAIStreaming(api_key="test-key", config=FakeConfig())


def test_every_connection_parameter_is_sent():
    """Verified live 2026-09-26: AssemblyAI accepted exactly this URL."""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(adapter().url()).query)
    assert query["sample_rate"] == ["16000"]
    assert query["encoding"] == ["pcm_s16le"]
    assert query["speech_model"] == ["universal-streaming-english"]
    assert query["format_turns"] == ["true"]
    # Push-to-talk owns the turn: the model must not end it early.
    assert query["end_of_turn_confidence_threshold"] == ["1.0"]
    assert query["max_turn_silence"] == ["2500"]


def test_keyterms_are_a_json_array_within_the_documented_limits():
    import json
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(adapter().url()).query)
    terms = json.loads(query["keyterms_prompt"][0])
    assert isinstance(terms, list) and terms
    assert len(terms) <= 100                      # documented ceiling
    assert all(isinstance(t, str) and len(t) <= 50 for t in terms)


def test_start_does_not_open_a_socket():
    """Billing counts idle connection time, so nothing opens until a key press."""
    made = adapter()
    asyncio.run(made.start())
    assert made.socket is None
    assert not made.connected()


def test_a_missing_key_fails_loudly():
    from voice.stt_assemblyai import AssemblyAIStreaming

    made = AssemblyAIStreaming(api_key="", config=FakeConfig())
    with pytest.raises(RuntimeError):
        asyncio.run(made.start())


def test_a_final_turn_resolves_and_partials_queue():
    made = adapter()

    async def scenario():
        await made.begin_utterance()
        made.on_turn({"type": "Turn", "transcript": "find flights", "end_of_turn": False})
        made.on_turn({"type": "Turn", "transcript": "find flights to Mumbai", "end_of_turn": True})
        assert made.final.done() and made.final.result() == "find flights to Mumbai"
        assert made.partial_queue.get_nowait() == "find flights"

    asyncio.run(scenario())


def test_the_formatted_turn_wins():
    made = adapter()

    async def scenario():
        await made.begin_utterance()
        made.on_turn({"type": "Turn", "transcript": "find flights to mumbai", "end_of_turn": True})
        made.on_turn({"type": "Turn", "utterance": "Find flights to Mumbai.", "end_of_turn": True,
                      "turn_is_formatted": True})
        assert made.formatted.result() == "Find flights to Mumbai."

    asyncio.run(scenario())


def test_the_mic_buffers_frames_while_the_socket_opens():
    """Capture starts before the handshake, so the first word is never lost."""

    async def scenario():
        mic = Microphone(asyncio.get_running_loop(), sample_rate=16000, frame_ms=60)
        mic.begin()
        for _ in range(12):            # ~0.7 s of speech during a ~1 s connect
            mic._put(b"\x00\x00" * 480)
        frames = await mic.drain()
        assert len(frames) == 12

    asyncio.run(scenario())


def test_the_mic_drops_rather_than_blocking_the_audio_thread():
    async def scenario():
        mic = Microphone(asyncio.get_running_loop(), queue_size=4)
        mic.begin()
        for _ in range(20):
            mic._put(b"\x00\x00")      # must not raise
        assert mic.queue.qsize() == 4

    asyncio.run(scenario())
