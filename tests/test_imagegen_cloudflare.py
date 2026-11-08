"""Cloudflare's FLUX schnell refuses width/height (400 "unevaluated properties"); the request sends prompt + steps only."""
import base64

from shorts_pipeline import imagegen


class _Resp:
    status_code = 200
    headers = {"content-type": "application/json"}

    def json(self):
        return {"result": {"image": base64.b64encode(b"jpegbytes").decode()}}


def test_flux_request_has_no_size_and_centres_the_subject(monkeypatch, tmp_path):
    sent = {}
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setattr(imagegen, "settings", lambda: {"provider": "cloudflare", "model": ""})
    monkeypatch.setattr(imagegen.requests, "post", lambda url, headers, json, timeout: sent.update(url=url, body=json) or _Resp())
    out = imagegen._cloudflare("A fox. cinematic, vertical composition, no text", tmp_path / "a.jpg")
    assert out.read_bytes() == b"jpegbytes"
    assert set(sent["body"]) == {"prompt", "steps"} and "flux-1-schnell" in sent["url"]
    assert "centred" in sent["body"]["prompt"] and "vertical composition" not in sent["body"]["prompt"]
