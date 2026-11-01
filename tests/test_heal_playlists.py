"""Self-healing render (findings become a fix and a second render) and auto playlists."""
from pathlib import Path

import httplib2
from googleapiclient.errors import HttpError

from shorts_pipeline import footage, playlists, render_check
from shorts_pipeline.storage import load_json, save_json


def test_plan_fix_maps_findings_to_lines_and_caption_style(monkeypatch):
    monkeypatch.setattr(render_check, "config", lambda: dict(render_check.DEFAULTS))
    segs = [{"path": "a", "start": 0.0, "end": 3.0, "line": 0}, {"path": "b", "start": 3.0, "end": 6.0, "line": 1},
            {"path": "c", "start": 6.0, "end": 10.0, "line": 2}]
    result = {"frames": [{"at": 0.5, "blank": 0.9, "covers_subject": 0.1, "text_cut": 0.0}], "black_spans": [(8.2, 9.0)],
              "issues": ["blank frame at 50%", "0.8 s of black picture", "captions cover the subject in most frames"]}
    fix = render_check.plan_fix(result, segs, duration=10.0, intro_seconds=0.0, style={"position": "lower", "size": 96})
    assert fix["segments"] == [1, 2] and fix["style"] == {"position": "bottom"}
    assert any("line 2" in r for r in fix["reasons"]) and any("moved from lower to bottom" in r for r in fix["reasons"])
    # an intro shifts the clock; at the bottom already, captions shrink instead; cut-off text shrinks too
    fix2 = render_check.plan_fix({"frames": [{"at": 0.5, "blank": 0.9}], "black_spans": [],
                                  "issues": ["captions cover the subject in most frames", "text is cut off at the frame edge"]},
                                 segs, duration=11.0, intro_seconds=1.0, style={"position": "bottom", "size": 100})
    assert fix2["segments"] == [1] and fix2["style"]["size"] == int(int(100 * 0.88) * 0.88)
    assert render_check.plan_fix({"frames": [], "black_spans": [], "issues": []}, segs, 10.0, 0.0, {"position": "lower"}) is None


def test_replan_line_skips_everything_already_in_the_short(monkeypatch, tmp_path):
    seen = {}

    def fake_pick(line_text, keyword, seconds, work_dir, index, **kw):
        seen.update(line=line_text, keyword=keyword, seconds=seconds, index=index, used=set(kw["used"]), subject=kw.get("named_subject"))
        kw["used"].add("new-clip")
        return tmp_path / f"bg_{index}.mp4", "video 'another clip' (80% literal fit)"
    monkeypatch.setattr(footage, "footage_for_line", fake_pick)
    script = {"title": "Octopus Gardens", "hook": "Hook.", "cta": "And then",
              "lines": [{"text": "Line one.", "visual_keyword": "octopus swimming"}, {"text": "Line two.", "visual_keyword": "deep sea vent", "named_subject": "Davidson Seamount"}],
              "footage_used": ["clip-1", "clip-2"]}
    segs = [{"path": "x", "start": 0.0, "end": 2.0, "line": 0}, {"path": "y", "start": 2.0, "end": 5.0, "line": 1},
            {"path": "z", "start": 5.0, "end": 8.0, "line": 2}]
    new, note = footage.replan_line(script, segs, 2, tmp_path)
    assert seen["index"] == 2 and seen["keyword"] == "deep sea vent" and seen["line"] == "Line two." and seen["seconds"] == 3.0
    assert seen["used"] == {"clip-1", "clip-2"} and seen["subject"] == "Davidson Seamount"
    assert new["path"].endswith("bg_2.mp4") and new["start"] == 5.0 and new["line"] == 2 and "another clip" in note
    assert script["footage_used"] == ["clip-1", "clip-2", "new-clip"]


class _YT:
    def __init__(self, fail_once=False):
        self.created, self.items, self.fail_once = [], [], fail_once

    def playlists(self):
        return self

    def playlistItems(self):
        return self

    def insert(self, part, body):
        yt = self

        class _Req:
            def execute(self_inner):
                if "resourceId" in body.get("snippet", {}):
                    if yt.fail_once:
                        yt.fail_once = False
                        raise HttpError(httplib2.Response({"status": 404}), b'{"error": {"errors": [{"reason": "playlistNotFound"}]}}')
                    yt.items.append((body["snippet"]["playlistId"], body["snippet"]["resourceId"]["videoId"]))
                    return {"id": "item"}
                yt.created.append(body["snippet"]["title"])
                return {"id": f"PL{len(yt.created)}"}
        return _Req()


def test_playlists_create_add_and_backfill_a_series(monkeypatch, tmp_path):
    monkeypatch.setattr(playlists, "STATE_PATH", tmp_path / "playlists.json")
    monkeypatch.setattr(playlists, "config", lambda: {**playlists.DEFAULTS, "backfill_per_upload": 2})
    monkeypatch.setattr("shorts_pipeline.series.parts", lambda series, output_dir=None, exclude=None: [
        {"dir": "d1", "youtube_id": "v1", "title": "p1", "part": 1, "privacy": "public"},
        {"dir": "d2", "youtube_id": "v2", "title": "p2", "part": 2, "privacy": "public"},
        {"dir": "d3", "youtube_id": "v3", "title": "p3", "part": 3, "privacy": "deleted"},
        {"dir": "d4", "youtube_id": "v4", "title": "p4", "part": 4, "privacy": "public"}])
    yt = _YT()
    out = tmp_path / "d4"
    out.mkdir()
    meta = {"youtube_id": "v4", "blueprint": {"series": "bible stories"}, "title": "p4"}
    save_json(out / "meta.json", meta)
    r = playlists.after_upload(out, meta, yt=yt)
    assert r == {"title": "Bible Stories", "added": True, "backfilled": 2}
    assert yt.created == ["Bible Stories"] and [v for _, v in yt.items] == ["v4", "v1", "v2"]     # the deleted part skipped, cap 2
    assert load_json(out / "meta.json")["playlist"] == "Bible Stories"
    assert load_json(tmp_path / "playlists.json")["playlists"]["Bible Stories"]["items"] == ["v4", "v1", "v2"]
    # the same upload again adds nothing; a compilation goes to its own playlist; a plain Short has none
    assert playlists.after_upload(out, meta, yt=yt)["added"] is False
    assert playlists.title_for({"mode": "compilation"}) == "Weekly compilations"
    assert playlists.title_for({"blueprint": {"format": "explainer", "topic": "space"}}) is None


def test_playlist_deleted_on_youtube_is_made_again(monkeypatch, tmp_path):
    monkeypatch.setattr(playlists, "STATE_PATH", tmp_path / "playlists.json")
    monkeypatch.setattr(playlists, "config", lambda: dict(playlists.DEFAULTS))
    save_json(tmp_path / "playlists.json", {"playlists": {"Space Facts": {"id": "PLold", "items": ["v0"]}}})
    yt = _YT(fail_once=True)
    assert playlists.add("v9", "Space Facts", yt) is True
    assert yt.created == ["Space Facts"] and yt.items == [("PL1", "v9")]
    st = load_json(tmp_path / "playlists.json")["playlists"]["Space Facts"]
    assert st["id"] == "PL1" and st["items"] == ["v9"]
