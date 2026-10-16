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
    monkeypatch.setattr(tts, "EDGE_WAITS", (0, 0))
    tries = []
    monkeypatch.setattr(tts, "synthesize_edge", lambda *a, **k: tries.append(1) or (_ for _ in ()).throw(RuntimeError("NoAudioReceived")))
    monkeypatch.setattr(tts, "kokoro_available", lambda: True)
    monkeypatch.setattr(tts, "synthesize_kokoro", lambda text, out, **k: [{"text": "ok", "start": 0, "end": 1}])
    assert tts.synthesize("Hi.", tmp_path / "v.mp3", voice="en-US-AndrewNeural")[0]["text"] == "ok"
    assert len(tries) == 3                                       # edge-tts was given three chances first
    monkeypatch.setattr(tts, "kokoro_available", lambda: False)
    monkeypatch.setattr(tts, "download_kokoro", lambda: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(RuntimeError, match="NoAudioReceived"):
        tts.synthesize("Hi.", tmp_path / "v.mp3", voice="en-US-AndrewNeural")


def test_edge_recovers_on_a_later_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "config", lambda: {"engine": "edge", "fallback": "kokoro", "kokoro_voice": "am_michael"})
    monkeypatch.setattr(tts, "EDGE_WAITS", (0, 0))
    tries = []

    def edge(*a, **k):
        tries.append(1)
        if len(tries) < 2:
            raise RuntimeError("WSServerHandshakeError: 403")
        return [{"text": "edge", "start": 0, "end": 1}]
    monkeypatch.setattr(tts, "synthesize_edge", edge)
    assert tts.synthesize("Hi.", tmp_path / "v.mp3", voice="en-US-AndrewNeural")[0]["text"] == "edge"


def test_kokoro_is_fetched_the_first_time_it_is_needed(monkeypatch):
    fetched = []
    monkeypatch.setattr(tts, "kokoro_available", lambda: bool(fetched))
    monkeypatch.setattr(tts, "download_kokoro", lambda: fetched.append(1))
    pytest.importorskip("kokoro_onnx")
    assert tts.ensure_kokoro() and fetched == [1]


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


def test_setting_and_place_rules_for_footage():
    """Regression: an Antarctica line once got green, tree-covered volcano clips and viewers called it out."""
    green = {"id": "g", "description": "majestic volcano erupting with smoke and ash"}
    etna = {"id": "e", "description": "spectacular mount etna eruption plume"}
    ice = {"id": "i", "description": "snowy antarctic volcano"}
    probs, oks = {"g": .5, "e": .3, "i": .2}, {"g": .8, "e": .8, "i": .7}
    conflicts = {"g": .1, "e": .9, "i": .1}
    # a line set in polar ice: only the clip that shows ice may be used
    pick, _ = footage._pick_with_variety([green, etna, ice], probs, oks, False, set(), needs_setting=.9,
                                         settings={"g": .1, "e": .1, "i": .8}, conflicts=conflicts)
    assert pick["id"] == "i"
    # nothing shows the setting: no clip at all (the line falls back to an AI image)
    assert footage._pick_with_variety([green, etna], probs, oks, False, set(), needs_setting=.9,
                                      settings={"g": .1, "e": .1}, conflicts=conflicts) == (None, 0.0)
    # a line about Krakatoa (no visible environment): a generic eruption is fine, a different named volcano is not
    pick, _ = footage._pick_with_variety([etna, green], probs, oks, False, set(), needs_setting=.1,
                                         settings={"g": .1, "e": .1}, conflicts=conflicts)
    assert pick["id"] == "g"


# ------------------------------------------------------------------ real photos of named things (Wikimedia Commons)
def test_only_free_licences_are_used():
    from shorts_pipeline import commons
    for ok in ("CC BY-SA 4.0", "CC BY 2.0", "Public domain", "CC0", "cc-by-sa-3.0"):
        assert commons.free_licence(ok), ok
    for bad in ("CC BY-NC 2.0", "CC BY-ND 4.0", "CC BY-NC-SA 3.0", "Fair use", "All rights reserved", ""):
        assert not commons.free_licence(bad), bad


def test_named_subject_line_uses_a_credited_commons_photo(monkeypatch, tmp_path):
    from shorts_pipeline import commons
    photo = {"id": "wc1", "kind": "photo", "source": "commons", "description": "Bete Giyorgis, Lalibela",
             "link": "https://upload.wikimedia.org/x.jpg", "landscape": True,
             "credit": {"title": "Bete Giyorgis", "author": "A. Savin", "licence": "CC BY-SA 3.0", "url": "https://c/1"}}
    monkeypatch.setattr(commons, "search", lambda subject: [photo])
    monkeypatch.setattr(commons, "choose", lambda line, subject, cands, used=None: (cands[0], 0.95))
    made = []
    monkeypatch.setattr(footage, "photo_clip", lambda url, pid, sec, out, whole=False: made.append(whole) or out)
    monkeypatch.setattr(footage, "video_candidates", lambda kw: pytest.fail("stock search must not run"))
    credits, used = [], set()
    clip, note = footage.footage_for_line("A church carved from one rock.", "rock church", 3.0, tmp_path, 0,
                                          used=used, named_subject="Church of Saint George, Lalibela", credits=credits)
    assert "Wikimedia Commons" in note and made == [True] and used == {"wc1"}
    line = commons.credit_line(credits + credits)                  # the same photo twice is credited once
    assert line.count("A. Savin (CC BY-SA 3.0) https://c/1") == 1


def test_hook_and_closing_line_show_the_main_named_subject(monkeypatch, tmp_path):
    monkeypatch.setenv("PEXELS_API_KEY", "test")
    seen = []
    monkeypatch.setattr(footage, "footage_for_line",
                        lambda chunk, kw, sec, wd, i, **k: seen.append(k["named_subject"]) or (tmp_path / f"{i}.mp4", "x"))
    script = {"hook": "Its roof is at ground level.", "cta": "And you'd walk right past it.",
              "lines": [{"text": "It is Saint George's church.", "visual_keyword": "rock church",
                         "named_subject": "Church of Saint George, Lalibela"},
                        {"text": "Workers cut down into rock.", "visual_keyword": "stone carving", "named_subject": ""}]}
    words = [{"text": w, "start": i * 0.4, "end": i * 0.4 + 0.3}
             for i, w in enumerate(" ".join([script["hook"], *[l["text"] for l in script["lines"]], script["cta"]]).split())]
    footage.plan_backgrounds(script, words, words[-1]["end"] + 0.5, "pexels", tmp_path)
    lalibela = "Church of Saint George, Lalibela"
    assert seen == [lalibela, lalibela, "", lalibela]     # hook, line 1, a generic line, closing line


# ------------------------------------------------------------------ image check of the picked footage (vision.py)
def test_images_are_prepared_within_typesafe_limits():
    import io
    from PIL import Image as PILImage
    from shorts_pipeline import vision
    for size in ((3000, 2000), (1080, 1920), (200, 150), (4000, 300)):
        buf = io.BytesIO()
        PILImage.new("RGB", size, "red").save(buf, "PNG")
        w, h = PILImage.open(io.BytesIO(vision.prepare(buf.getvalue()))).size
        assert 280 <= w <= 1568 and 280 <= h <= 1568, (size, (w, h))


def _vision_mode(monkeypatch, mode, judged):
    from shorts_pipeline import vision
    monkeypatch.setattr(vision, "config", lambda: dict(vision.DEFAULTS, mode=mode))
    monkeypatch.setattr(vision, "enabled", lambda: mode != "off")
    monkeypatch.setattr(vision, "judge", lambda line, subject, pick, clip=None, topic="": judged[pick["id"]])


def test_a_clip_the_image_check_rejects_makes_room_for_the_next(monkeypatch, tmp_path):
    forest = {"id": "1", "kind": "video", "description": "volcano", "link": "x", "preview": "p1"}
    ice = {"id": "2", "kind": "video", "description": "volcano", "link": "y", "preview": "p2"}
    monkeypatch.setattr(footage, "video_candidates", lambda kw: [forest, ice])
    monkeypatch.setattr(footage, "photo_candidates", lambda kw: [])
    monkeypatch.setattr(footage, "choose", lambda line, kw, cands, strict=True, used=None: (cands[0], 0.9) if cands else (None, 0.0))
    monkeypatch.setattr(footage, "_materialize", lambda pick, kind, sec, wd, i: tmp_path / f"{pick['id']}.mp4")
    judged = {"1": {"shows": 0.9, "conflict": 0.8}, "2": {"shows": 0.8, "conflict": 0.1}}
    _vision_mode(monkeypatch, "on", judged)
    clip, note = footage.footage_for_line("Antarctica's volcano erupts through ice.", "antarctica volcano ice", 3.0, tmp_path, 0)
    assert clip.name == "2.mp4" and "image check" in note
    _vision_mode(monkeypatch, "log", judged)                        # log mode: judged, never rejected
    clip, note = footage.footage_for_line("Antarctica's volcano erupts through ice.", "antarctica volcano ice", 3.0, tmp_path, 0)
    assert clip.name == "1.mp4" and "would reject" in note


def test_ai_image_competes_with_a_weak_clip_but_never_for_a_real_place(monkeypatch, tmp_path):
    from shorts_pipeline import imagegen, vision
    monkeypatch.setattr(imagegen, "settings", lambda: {"enabled": True, "mode": "compete", "provider": "pollinations",
                                                       "compete_below": 0.85, "compete_margin": 0.1, "max_per_video": 4})
    monkeypatch.setattr(imagegen, "available", lambda provider=None: True)
    img = tmp_path / "ai.jpg"
    img.write_bytes(b"x")
    monkeypatch.setattr(imagegen, "generate", lambda *a, **k: img)
    monkeypatch.setattr(vision, "enabled", lambda: True)
    monkeypatch.setattr(vision, "config", lambda: dict(vision.DEFAULTS, mode="on"))
    monkeypatch.setattr(vision, "judge", lambda *a, **k: {"shows": 0.9, "conflict": 0.05, "off_topic": 0.05})
    monkeypatch.setattr(footage, "image_clip", lambda im, sec, out, whole=False: out)
    footage._AI_BUDGET["left"] = 2
    won = footage._ai_challenger("A bird pulls a leech.", "bird pulling leech", "", "bird pulling leech", "", 0.4,
                                 3.0, tmp_path, 0, "Lion and bird")
    assert won and "beat the best clip" in won[1]
    assert footage._ai_challenger("x", "k", "", "k", "", 0.95, 3.0, tmp_path, 1, "t") is None        # strong clip kept
    assert footage._ai_challenger("x", "k", "", "k", "Mount Erebus", 0.2, 3.0, tmp_path, 2, "t") is None  # real place
    monkeypatch.setattr(vision, "judge", lambda *a, **k: {"shows": 0.45, "conflict": 0.05, "off_topic": 0.05})
    assert footage._ai_challenger("x", "k", "", "k", "", 0.4, 3.0, tmp_path, 3, "t") is None          # not better enough


def test_ai_images_only_never_uses_internet_footage(monkeypatch, tmp_path):
    from shorts_pipeline import imagegen
    imagegen.override("only")
    try:
        monkeypatch.setattr(imagegen, "available", lambda provider=None: True)
        monkeypatch.setattr(footage, "video_candidates", lambda kw: pytest.fail("no internet footage in AI-only mode"))
        monkeypatch.setattr("shorts_pipeline.commons.search", lambda s: pytest.fail("no Commons photos either"))
        made = []
        monkeypatch.setattr(footage, "image_clip", lambda im, sec, out, whole=False: made.append(im) or out)
        img = tmp_path / "pollinations_abc.jpg"
        img.write_bytes(b"x")
        monkeypatch.setattr(imagegen, "generate", lambda *a, **k: img)
        clip, note = footage.footage_for_line("Lalibela's church was carved from rock.", "rock church", 3.0, tmp_path, 0,
                                              named_subject="Church of Saint George, Lalibela")
        assert "AI image (pollinations)" in note and made == [img]
        monkeypatch.setattr(imagegen, "generate", lambda *a, **k: None)
        monkeypatch.setattr(footage, "generated_background", lambda out, sec, seed=0: out)
        clip, note = footage.footage_for_line("x", "k", 3.0, tmp_path, 1)
        assert note.startswith("generated background")
    finally:
        imagegen.override(None)


def test_director_prompt_replaces_the_footage_term_and_drops_fixed_lighting(monkeypatch, tmp_path):
    from shorts_pipeline import imagegen
    assert imagegen._look("cinematic photograph, natural light, realistic, sharp detail") == "cinematic photograph, realistic, sharp detail"
    seen = []
    monkeypatch.setattr(imagegen, "settings", lambda: {"enabled": True, "mode": "only", "provider": "pollinations",
                                                       "style": "cinematic photograph, natural light, realistic"})
    monkeypatch.setattr(imagegen, "available", lambda provider=None: True)
    monkeypatch.setattr(imagegen, "AIIMG_DIR", tmp_path)
    monkeypatch.setitem(imagegen.PROVIDERS, "pollinations", lambda prompt, dest: seen.append(prompt) or dest.write_bytes(b"x" * 2000))
    imagegen.generate("Millions of firefly squid turn a bay blue.", "firefly squid glowing japan",
                      prompt="Toyama Bay at night, surf glowing electric blue with tiny firefly squid")
    assert seen[0].startswith("Toyama Bay at night") and "natural light" not in seen[0] and "illustrating" not in seen[0]
    imagegen.generate("x", "firefly squid close-up", prompt="Toyama Bay at night", variant=1)
    assert seen[1].startswith("firefly squid, clearly visible")          # second take: the subject first


def test_director_prompts_must_cover_every_line(monkeypatch):
    from shorts_pipeline import script_gen
    script = {"title": "T", "hook": "H.", "cta": "C.", "full_text": "H. A. B. C.", "visual_fallback": "v",
              "lines": [{"text": "A.", "visual_keyword": "a"}, {"text": "B.", "visual_keyword": "b"}]}
    monkeypatch.setattr(script_gen, "pick_backend", lambda pref: "claude_code")
    monkeypatch.setattr(script_gen, "_draft", lambda *a, **k: script_gen._ImagePrompts(items=["p1", "p2", "p3", "p4"]))
    assert script_gen.image_prompts(script) == ["p1", "p2", "p3", "p4"]
    monkeypatch.setattr(script_gen, "_draft", lambda *a, **k: script_gen._ImagePrompts(items=["p1"]))
    assert script_gen.image_prompts(script) == []                   # misaligned: fall back to the footage terms
