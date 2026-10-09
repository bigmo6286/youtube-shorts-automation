"""Kokoro fallback voice, Pixabay footage and no-repeat clip choice, comment engagement."""
from types import SimpleNamespace

import pytest

from shorts_pipeline import comments, footage, tts
from shorts_pipeline.storage import load_json, save_json


# ------------------------------------------------------------------ voice
def test_words_are_spread_across_the_sentence_with_pauses_after_commas():
    w = tts._spread_words("Octopuses have three hearts, and one stops.", 1.0, 4.0)
    assert [x["text"] for x in w] == ["Octopuses", "have", "three", "hearts", "and", "one", "stops"]
    assert w[0]["start"] == 1.0 and w[-1]["end"] <= 4.0
    assert all(a["end"] <= b["start"] for a, b in zip(w, w[1:]))
    gap_after_comma = w[4]["start"] - w[3]["end"]
    assert gap_after_comma > w[1]["start"] - w[0]["end"]


def test_speed_follows_the_edge_rate():
    assert tts._speed("+8%") == pytest.approx(1.08) and tts._speed("-20%") == pytest.approx(0.8) and tts._speed("") == 1.0


def test_kokoro_takes_over_when_edge_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "config", lambda: {"engine": "edge", "fallback": "kokoro", "kokoro_voice": "am_michael"})
    monkeypatch.setattr(tts, "synthesize_edge", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("NoAudioReceived")))
    monkeypatch.setattr(tts, "kokoro_available", lambda: True)
    monkeypatch.setattr(tts, "synthesize_kokoro", lambda text, out, **k: [{"text": "ok", "start": 0, "end": 1}])
    assert tts.synthesize("Hi.", tmp_path / "v.mp3", voice="x")[0]["text"] == "ok"
    monkeypatch.setattr(tts, "kokoro_available", lambda: False)
    with pytest.raises(RuntimeError, match="NoAudioReceived"):
        tts.synthesize("Hi.", tmp_path / "v.mp3", voice="x")


# ------------------------------------------------------------------ footage
def test_pixabay_results_become_candidates(monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "test")
    monkeypatch.setattr(footage._SEARCH_CACHE, "get", lambda k: None)
    monkeypatch.setattr(footage._SEARCH_CACHE, "set", lambda k, v: None)
    hit = {"id": 42, "tags": "octopus, sea, underwater", "duration": 12, "pageURL": "https://pixabay.com/v/42",
           "videos": {"large": {"url": "https://cdn/l.mp4", "width": 1920, "height": 1080},
                      "medium": {"url": "https://cdn/m.mp4", "width": 1280, "height": 720},
                      "tiny": {"url": "", "width": 0, "height": 0}}}
    monkeypatch.setattr(footage, "_pixabay", lambda path, params: [hit])
    c = footage.search_pixabay_videos("octopus")[0]
    assert c["id"] == "pb42" and c["link"] == "https://cdn/l.mp4" and "cropped to vertical" in c["description"]


def test_a_clip_already_used_is_only_a_last_resort(monkeypatch, tmp_path):
    a = {"id": "A", "kind": "video", "description": "octopus", "link": "a"}
    b = {"id": "B", "kind": "video", "description": "octopus close", "link": "b"}
    monkeypatch.setattr("shorts_pipeline.imagegen.settings", lambda: {"enabled": False})
    monkeypatch.setattr(footage, "photo_candidates", lambda kw: [])
    monkeypatch.setattr(footage, "video_candidates", lambda kw: [a] if kw == "octopus swimming" else [b])
    monkeypatch.setattr(footage, "choose", lambda line, kw, cands, strict=True, used=None: (cands[0], 0.9) if cands else (None, 0))
    monkeypatch.setattr(footage, "_materialize", lambda pick, kind, s, wd, i: tmp_path / f"{pick['id']}.mp4")
    clip, note = footage.footage_for_line("It swims.", "octopus swimming", 3, tmp_path, 1, "octopus", used={"A"})
    assert clip.name == "B.mp4"                      # the used clip A was skipped for the subject term's clip B


# ------------------------------------------------------------------ comments
class _Exec:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _YT:
    def __init__(self, threads):
        self.threads, self.inserted = threads, []

    def channels(self):
        return SimpleNamespace(list=lambda **k: _Exec({"items": [{"id": "ME"}]}))

    def commentThreads(self):
        def insert(part, body):
            self.inserted.append(body)
            return _Exec({"id": "q1"})
        return SimpleNamespace(list=lambda **k: _Exec({"items": self.threads}), insert=insert)


def _thread(tid, author, text, video="v1"):
    return {"id": tid, "snippet": {"videoId": video, "topLevelComment": {"snippet": {
        "authorChannelId": {"value": author}, "authorDisplayName": author, "textDisplay": text, "publishedAt": tid}}}}


@pytest.fixture
def comment_env(tmp_path, monkeypatch):
    monkeypatch.setattr(comments, "STATE_PATH", tmp_path / "comments.json")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(comments, "OUTPUT_DIR", out)
    monkeypatch.setattr(comments, "connected", lambda: True)
    monkeypatch.setattr(comments, "config", lambda: dict(comments.DEFAULTS))
    monkeypatch.setattr("shorts_pipeline.notify.telegram_configured", lambda: False)
    return out


def test_comments_are_sorted_and_replies_drafted(comment_env, monkeypatch):
    yt = _YT([_thread("t1", "viewer1", "Wow, I never knew octopuses had three hearts!"),
              _thread("t2", "spammer", "check my channel!!!"), _thread("t3", "brand", "Collab? email me"),
              _thread("t4", "ME", "my own comment")])
    monkeypatch.setattr(comments, "_service", lambda interactive=False: yt)

    def triage(items, ctx):
        verdicts = {"t1": (0, 0, .9), "t2": (.95, 0, 0), "t3": (0, .9, .2)}
        for it in items:
            it["spam"], it["needs_owner"], it["worth"] = verdicts[it["id"]]
    monkeypatch.setattr(comments, "triage", triage)
    monkeypatch.setattr(comments, "draft_replies", lambda items, ctx: [it.update(draft="They do, all three!") for it in items])
    s = comments.run()
    assert (s["new"], s["drafted"], s["spam"], s["needs_owner"], s["posted"]) == (3, 1, 1, 1, 0)   # auto_reply is off
    statuses = {k: v["status"] for k, v in load_json(comments.STATE_PATH)["threads"].items()}
    assert statuses == {"t1": "drafted", "t2": "spam", "t3": "needs_owner"}
    assert {c["id"] for c in comments.pending()} == {"t1", "t3"}
    assert comments.fetch_new(yt) == []                                    # already seen: not judged twice


def test_question_comment_is_posted_once_for_public_shorts(comment_env):
    for name, privacy in (("a", "public"), ("b", "private")):
        d = comment_env / name
        d.mkdir()
        save_json(d / "meta.json", {"youtube_id": f"vid_{name}", "privacy": privacy, "title": name})
        save_json(d / "script.json", {"comment_question": "Which fact surprised you?"})
    yt = _YT([])
    assert comments.post_questions(yt) == 1 and yt.inserted[0]["snippet"]["videoId"] == "vid_a"
    assert comments.post_questions(yt) == 0                                # not twice


def test_word_timings_are_plain_floats_that_json_can_save(tmp_path):
    import numpy as np
    w = tts._spread_words("One two three.", np.float64(0.5), np.float64(2.0))
    save_json(tmp_path / "w.json", w)                    # numpy floats used to break saving words.json
    assert load_json(tmp_path / "w.json")[0]["start"] == 0.5
