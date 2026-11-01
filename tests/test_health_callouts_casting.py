"""Channel health watch, key-fact callouts, caption track, image provider failover, voice casting, word alignment."""
import base64
from pathlib import Path

from shorts_pipeline import align, callouts, captions, casting, health, imagegen, upload
from shorts_pipeline.storage import load_json


class _A:
    def __init__(self, **k):
        self.__dict__.update(k)


def _ts_client(answers):
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def system_one(self, state, questions):
            return _A(answers={k: v for k, v in answers.items() if k in questions})
    return _C


# ---------------------------------------------------------------------------------------------------- health

def _video(i, age_h, vph, views=1000, likes=50, **extra):
    return {"id": f"v{i}", "title": f"Short {i}", "privacy": "public", "age_hours": age_h, "public_hours": age_h,
            "views_per_hour": vph, "views": views, "likes": likes, **extra}


def test_health_finds_drops_restrictions_and_vanished_uploads():
    cfg = dict(health.DEFAULTS)
    before = [_video(i, 100 + i * 20, 10.0) for i in range(6)]
    fine = before + [_video(20 + i, 24 + i * 10, 9.0) for i in range(3)]
    assert health.check(fine, cfg=cfg) == []
    bad = before + [_video(20 + i, 24 + i * 10, 2.0, likes=2) for i in range(3)]
    keys = [f["key"] for f in health.check(bad, known_ids=["v0", "gone1"], cfg=cfg)]
    assert keys == ["gone:gone1", "views_drop", "likes_drop"]
    restricted = fine + [_video(99, 30, 9.0, age_restricted=True), _video(98, 30, 9.0, upload_status="rejected", rejection_reason="copyright")]
    found = {f["key"]: f["text"] for f in health.check(restricted, cfg=cfg)}
    assert "age-restricted" in found["video:v99"] and "rejected it (copyright)" in found["video:v98"]


def test_health_sync_alerts_once_a_day_and_remembers_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(health, "STATE_PATH", tmp_path / "health.json")
    monkeypatch.setattr(health, "config", lambda: dict(health.DEFAULTS))
    sent = []
    monkeypatch.setattr("shorts_pipeline.alerts.send", lambda key, text, repeat_hours=3: sent.append((key, repeat_hours)) or True)
    vids = [_video(i, 100 + i * 20, 10.0) for i in range(6)] + [_video(7, 30, 1.0)] * 3
    line = health.sync(vids)
    assert "views_drop" in str(sent) and sent[0][1] == 24 and "1 finding" in line
    assert health.overview()["findings"][0]["key"] == "views_drop" and "v7" in load_json(tmp_path / "health.json")["known_ids"]
    assert health.report_lines()[0].startswith("Health: 1 finding")


# ---------------------------------------------------------------------------------------------------- callouts

def _script():
    return {"hook": "Three word hook.", "lines": [{"text": f"Line {i} weighs {i} billion tons."} for i in range(5)], "cta": "And then",
            "interrupt": {"line": 2, "card_text": "SOMETHING"}}


def test_callouts_are_checked_and_timed(monkeypatch):
    import typesafe_sdk
    monkeypatch.setattr(callouts, "config", lambda: dict(callouts.DEFAULTS))
    monkeypatch.setattr(callouts, "has_typesafe", lambda: True)
    monkeypatch.setattr(callouts, "_ask_claude", lambda script, skip, backend: [
        {"line": 1, "text": "1 BILLION TONS"}, {"line": 3, "text": "A MADE-UP CLAIM"}, {"line": 4, "text": "4 BILLION TONS"}])
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _ts_client({
        "in_0": _A(noul=0.95), "key_0": _A(noul=0.9), "in_1": _A(noul=0.1), "key_1": _A(noul=0.9), "in_2": _A(noul=0.9), "key_2": _A(noul=0.8)}))
    s = callouts.pick(_script())
    assert s["callouts"] == [{"line": 1, "text": "1 BILLION TONS"}, {"line": 4, "text": "4 BILLION TONS"}]
    words = [{"text": "w", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(40)]
    # hook 3 words, lines of 6: line 1 starts at word 9 (4.5 s), line 4 at word 27 (13.5 s)
    assert callouts.times(s, words) == [(4.5, "1 BILLION TONS"), (13.5, "4 BILLION TONS")]
    assert callouts.times(s, words, avoid=[5.0]) == [(13.5, "4 BILLION TONS")]          # too close to the interrupt
    monkeypatch.setattr(callouts, "has_typesafe", lambda: False)
    assert "callouts" not in callouts.pick(_script())                                     # nothing on Claude's word alone


# ---------------------------------------------------------------------------------------------------- caption track

def test_srt_cues_follow_sentences(tmp_path):
    text = "This is one. And two more words here."
    words = [{"text": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(text.split())]
    srt = captions.write_srt(words, tmp_path / "c.srt", full_text=text, offset=1.0).read_text(encoding="utf-8")
    assert srt.startswith("1\n00:00:01,000 --> 00:00:02,450\nThis is one.\n\n2\n00:00:02,500 --> ")
    assert "And two more words here." in srt and srt.count("-->") == 2


def test_caption_upload_sends_the_srt(monkeypatch, tmp_path):
    seen = {}

    class _YT:
        def captions(self):
            return self

        def insert(self, **kw):
            seen.update(kw)
            return _A(execute=lambda: {"id": "cap1"})
    monkeypatch.setattr(upload, "youtube_service", lambda interactive=True: _YT())
    monkeypatch.setattr("googleapiclient.http.MediaFileUpload", lambda path, mimetype, resumable: ("media", path))
    srt = tmp_path / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n\n", encoding="utf-8")
    assert upload.upload_caption("vid1", srt) == "cap1"
    assert seen["body"]["snippet"] == {"videoId": "vid1", "language": "en", "name": "English", "isDraft": False}
    assert seen["media_body"] == ("media", str(srt))


# ---------------------------------------------------------------------------------------------------- image providers

def test_provider_order_fails_over_and_pauses(monkeypatch):
    keys = {"CLOUDFLARE_ACCOUNT_ID": "acc", "CLOUDFLARE_API_TOKEN": "tok", "HF_TOKEN": "", "TOGETHER_API_KEY": "", "OPENAI_API_KEY": ""}
    monkeypatch.setattr(imagegen, "env", lambda k: keys.get(k, ""))
    monkeypatch.setattr(imagegen, "_PAUSED_UNTIL", {})
    cfg = {"provider": "pollinations"}
    assert imagegen.provider_order(cfg, now=1000.0) == ["pollinations", "cloudflare"]
    imagegen._PAUSED_UNTIL["pollinations"] = 2000.0
    assert imagegen.provider_order(cfg, now=1000.0) == ["cloudflare"]
    assert imagegen.provider_order({"provider": "cloudflare"}, now=3000.0) == ["cloudflare", "pollinations"]
    keys["CLOUDFLARE_API_TOKEN"] = ""
    assert imagegen.provider_order({"provider": "cloudflare"}, now=3000.0) == ["pollinations"]


def test_cloudflare_answer_is_decoded(monkeypatch, tmp_path):
    keys = {"CLOUDFLARE_ACCOUNT_ID": "acc", "CLOUDFLARE_API_TOKEN": "tok"}
    monkeypatch.setattr(imagegen, "env", lambda k: keys.get(k, ""))
    monkeypatch.setattr(imagegen, "settings", lambda: {"model": ""})
    seen = {}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, json=json)
        return _A(status_code=200, headers={"content-type": "application/json"}, json=lambda: {"result": {"image": base64.b64encode(b"JPEGDATA").decode()}}, text="")
    monkeypatch.setattr(imagegen.requests, "post", fake_post)
    out = imagegen._cloudflare("a cat", tmp_path / "x.jpg")
    assert out.read_bytes() == b"JPEGDATA" and "accounts/acc/ai/run/@cf/black-forest-labs/flux-1-schnell" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer tok" and seen["json"]["prompt"] == "a cat"


# ---------------------------------------------------------------------------------------------------- voice casting

def test_casting_weighs_typesafe_choice_by_learned_retention(monkeypatch):
    import typesafe_sdk
    monkeypatch.setattr(casting, "config", lambda: {**casting.DEFAULTS, "voices": dict(casting.DEFAULTS["voices"])})
    monkeypatch.setattr(casting, "has_typesafe", lambda: True)
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _ts_client({
        "voice": _A(probabilities={"en-US-AndrewNeural": 0.5, "en-US-BrianNeural": 0.4, "en-US-ChristopherNeural": 0.1}),
        "pace": _A(choice="brisk")}))
    monkeypatch.setattr(casting, "voice_stats", lambda: {})
    c = casting.choose({"title": "t", "hook": "h", "full_text": "x"}, {"topic": "space"})
    assert c["voice"] == "en-US-AndrewNeural" and c["rate"] == "+14%" and c["pace"] == "brisk"
    # Andrew's viewers leave sooner than Brian's: the learned factor turns the choice
    monkeypatch.setattr(casting, "voice_stats", lambda: {"en-US-AndrewNeural": {"videos": 8, "median_view_pct": 60.0},
                                                         "en-US-BrianNeural": {"videos": 6, "median_view_pct": 95.0}})
    c2 = casting.choose({"title": "t", "hook": "h", "full_text": "x"}, {})
    assert c2["voice"] == "en-US-BrianNeural" and c2["factors"]["en-US-AndrewNeural"] == round(60 / 95, 3)
    monkeypatch.setattr(casting, "has_typesafe", lambda: False)
    assert casting.choose({"title": "t"}, {}) is None


# ---------------------------------------------------------------------------------------------------- word alignment

def test_alignment_takes_recognised_times_and_spreads_the_rest():
    text = "The octopus garden hides two thousand nests, deep below."
    rec = [{"word": "the", "start": 0.1, "end": 0.2}, {"word": "octopus", "start": 0.25, "end": 0.7}, {"word": "garden", "start": 0.75, "end": 1.1},
           {"word": "hides", "start": 1.2, "end": 1.5}, {"word": "thousand", "start": 1.9, "end": 2.3}, {"word": "nests", "start": 2.35, "end": 2.7},
           {"word": "deep", "start": 2.9, "end": 3.1}, {"word": "below", "start": 3.15, "end": 3.5}]
    words = align.align(text, rec)
    assert [w["text"] for w in words] == ["The", "octopus", "garden", "hides", "two", "thousand", "nests", "deep", "below"]
    assert words[1]["start"] == 0.25 and words[8]["end"] == 3.5
    two = words[4]
    assert 1.5 <= two["start"] < two["end"] <= 1.9                      # "two" (unrecognised) sits between its neighbours
    assert all(words[i]["start"] <= words[i + 1]["start"] for i in range(len(words) - 1))
    spread = [{"text": "x", "start": 0.0, "end": 1.0}]
    assert align.align(text, rec[:2], fallback=spread) == spread          # too few matches: the spread timings stay
