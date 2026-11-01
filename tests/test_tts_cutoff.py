"""A voice the network cut short is rejected (so the retries and the Kokoro fallback run) instead of becoming a 7-second Short."""
import pytest

from shorts_pipeline import tts

TEXT = "In 1966 a NASA engineer felt his own spit boil. Jim LeBlanc was testing a spacesuit inside a vacuum chamber in Houston."


def _fake_stream(n_words):
    async def _synth(text, voice, rate, out_path):
        out_path.write_bytes(b"\0" * 4000)
        return [{"text": f"w{i}", "start": i * 0.3, "end": i * 0.3 + 0.25} for i in range(n_words)]
    return _synth


def test_cut_off_edge_voice_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "_synth", _fake_stream(5))                    # 5 of 22 words arrived
    with pytest.raises(RuntimeError, match="cut off: 5 of 22 words"):
        tts.synthesize_edge(TEXT, tmp_path / "v.mp3", voice="en-US-BrianNeural")


def test_complete_edge_voice_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "_synth", _fake_stream(21))                   # edge skips a token now and then
    words = tts.synthesize_edge(TEXT, tmp_path / "v.mp3", voice="en-US-BrianNeural")
    assert len(words) == 21


def test_stalled_stream_times_out(monkeypatch, tmp_path):
    import asyncio

    async def never_ends(text, voice, rate, out_path):
        await asyncio.sleep(5)
        return []
    monkeypatch.setattr(tts, "_stream", never_ends)
    monkeypatch.setattr(tts, "EDGE_TIMEOUT", 0.05)
    with pytest.raises(RuntimeError, match="did not finish"):
        tts.synthesize_edge(TEXT, tmp_path / "v.mp3", voice="en-US-BrianNeural")
