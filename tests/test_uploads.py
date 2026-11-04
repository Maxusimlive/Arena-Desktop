"""Attachment uploads: Arena's two storage flows and their limits.

Evaluation (Battle/Direct/Side by Side): mint an upload URL, PUT the bytes,
then get a signed read URL -> experimental attachment {name, contentType, url}.
Agent Mode: content-addressed by SHA-256 -> /api/chat/workspace/cas/user/<hash>.
"""

import base64
import hashlib
import io

import pytest
from PIL import Image

from arena.web import app as web


def _png_bytes(size=None):
    """A real 1x1 RGB PNG, optionally padded after IEND to an exact size."""
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), (10, 20, 30)).save(buf, format="PNG")
    data = buf.getvalue()
    if size is not None:
        assert size >= len(data)
        data += b"x" * (size - len(data))
    return data


@pytest.fixture()
def uploads(monkeypatch):
    calls = {"arena": [], "put": []}

    def fake_post_json(path, body, timeout=60):
        calls["arena"].append((path, body))
        if path.endswith("generate-agent-upload-url"):
            return {"uploadUrl": "https://storage.example/put-agent", "key": "k-a"}, None, 200
        if path.endswith("generate-upload-url"):
            return {"uploadUrl": "https://storage.example/put", "key": "k-1"}, None, 200
        if path.endswith("get-signed-url"):
            assert body == {"key": "k-1"}
            return {"url": "https://storage.example/signed/a.png"}, None, 200
        raise AssertionError(f"unexpected arena call {path}")

    def fake_put(url, data, media_type):
        calls["put"].append((url, media_type, len(data)))
        return None

    monkeypatch.setattr(web, "_arena_post_json", fake_post_json)
    monkeypatch.setattr(web, "_storage_put", fake_put)
    monkeypatch.setattr(web, "_signed_in_somewhere", lambda client: True)
    monkeypatch.setattr(web, "get_client", lambda: object())
    return web.app.test_client(), calls


def _post(client, name, data, kind="evaluation", modality=None, content_type=None):
    form = {"file": (io.BytesIO(data), name, content_type or "application/octet-stream"),
            "kind": kind}
    if modality:
        form["modality"] = modality
    return client.post("/api/uploads", data=form)


def test_evaluation_upload_flow(uploads):
    client, calls = uploads
    data = _png_bytes()
    resp = _post(client, "cat.png", data)
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    assert out["ok"] is True and out["kind"] == "evaluation"
    assert out["name"] == out["filename"] == "cat.png"
    assert out["contentType"] == out["mediaType"] == "image/png"
    assert out["url"] == "https://storage.example/signed/a.png"
    assert out["size"] == len(data)
    assert out["image"] == {"format": "PNG", "width": 1, "height": 1,
                            "mode": "RGB", "animated": False, "normalized": False}
    assert calls["put"] == [("https://storage.example/put", "image/png", len(data))]
    paths = [p for p, _ in calls["arena"]]
    assert paths[0].endswith("generate-upload-url") and paths[1].endswith("get-signed-url")


def test_agent_upload_is_content_addressed(uploads):
    client, calls = uploads
    data = b"hello agent"
    resp = _post(client, "notes.md", data, kind="agent")
    assert resp.status_code == 200, resp.get_json()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
    out = resp.get_json()
    assert out["url"] == f"/api/chat/workspace/cas/user/{digest}"
    assert out["mediaType"] == "text/markdown" and out["filename"] == "notes.md"
    _, body = calls["arena"][0]
    assert body == {"hash": digest, "contentType": "text/markdown", "size": len(data)}
    assert len(digest) == 43


def test_evaluation_rejects_agent_only_types(uploads):
    client, _ = uploads
    resp = _post(client, "notes.md", b"# hi")
    assert resp.status_code == 400
    assert "PNG, JPEG, WebP and PDF" in resp.get_json()["error"]


def test_agent_rejects_python_files(uploads):
    client, _ = uploads
    resp = _post(client, "script.py", b"print(1)", kind="agent")
    assert resp.status_code == 400
    assert "Agent Mode accepts" in resp.get_json()["error"]


def test_size_limits_per_area(uploads):
    client, _ = uploads
    big = _png_bytes(16 * 1024 * 1024)
    assert _post(client, "photo.png", big).status_code == 400
    eleven = _png_bytes(11 * 1024 * 1024)
    assert _post(client, "photo.png", eleven, modality="video").status_code == 400
    assert _post(client, "photo.png", eleven).status_code == 200
    pdf = b"%PDF" + b"x" * (11 * 1024 * 1024)
    agent_pdf = _post(client, "doc.pdf", pdf, kind="agent")
    assert agent_pdf.status_code == 400
    assert "10 MB" in agent_pdf.get_json()["error"]


def test_upload_requires_sign_in(monkeypatch):
    monkeypatch.setattr(web, "_signed_in_somewhere", lambda client: False)
    monkeypatch.setattr(web, "get_client", lambda: object())
    resp = web.app.test_client().post("/api/uploads", data={})
    assert resp.status_code == 401


def test_request_attachments_sanitizes_and_caps():
    data = {"attachments": [
        {"name": "a.png", "contentType": "image/png", "url": "https://x/a.png", "size": 5, "junk": 1},
        "not-a-dict",
        {"filename": "b.pdf", "mediaType": "application/pdf", "url": "/api/chat/workspace/cas/user/x"},
    ]}
    out, err = web._request_attachments(data, native_agent_files=False)
    assert err is None
    assert out == [{"name": "a.png", "contentType": "image/png", "url": "https://x/a.png", "size": 5},
                   {"name": "b.pdf", "contentType": "application/pdf",
                    "url": "/api/chat/workspace/cas/user/x", "size": 0}]
    out, err = web._request_attachments(data, native_agent_files=True)
    assert err is None
    assert out[1] == {"filename": "b.pdf", "mediaType": "application/pdf",
                      "url": "/api/chat/workspace/cas/user/x", "size": 0}
    missing = {"attachments": [{"name": "a.png", "contentType": "image/png"}]}
    out, err = web._request_attachments(missing, native_agent_files=False)
    assert out is None and "upload link" in err


def test_media_type_prefers_extension_for_text_kinds():
    assert web._attachment_media_type("data.csv", "application/octet-stream") == "text/csv"
    assert web._attachment_media_type("page.html", "text/plain") == "text/html"
    assert web._attachment_media_type("cfg.json", "text/plain") == "application/json"
    assert web._attachment_media_type("photo.jpg", "") == "image/jpeg"


def test_upload_diagnostics_record_safe_metadata(uploads):
    client, _calls = uploads
    web._UPLOAD_DIAGNOSTICS.clear()
    data = _png_bytes()
    resp = client.post("/api/uploads", data={
        "file": (io.BytesIO(data), "very-secret-screenshot.png", "image/png"),
        "kind": "evaluation", "modality": "text", "source": "paste",
        "app_mode": "direct", "attempt": "2",
    })
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    entry = web._upload_diagnostics_snapshot()[-1]
    assert out["diagnostic_id"] == entry["id"]
    assert out["stage"] == entry["stage"] == "complete"
    assert entry["source"] == "paste"
    assert entry["app_mode"] == "direct"
    assert entry["attempt"] == 2
    assert entry["detected_type"] == entry["resolved_type"] == "image/png"
    assert entry["bytes_in"] == entry["bytes_out"] == len(data)
    assert entry["image"]["width"] == entry["image"]["height"] == 1
    rendered = "\n".join(web._upload_diagnostic_lines())
    assert "very-secret-screenshot" not in rendered
    assert "storage.example" not in rendered
    assert "signed/a.png" not in rendered


def test_upload_diagnostics_record_stage_and_arena_status(uploads, monkeypatch):
    client, _calls = uploads
    web._UPLOAD_DIAGNOSTICS.clear()
    monkeypatch.setattr(
        web, "_arena_post_json",
        lambda *_args, **_kwargs: (None, "Arena refused https://storage.example/x?sig=secret", 403))
    resp = client.post("/api/uploads", data={
        "file": (io.BytesIO(_png_bytes()), "private-name.png", "image/png"),
        "kind": "evaluation", "source": "drop", "app_mode": "battle", "attempt": "1",
    })
    assert resp.status_code == 502
    entry = web._upload_diagnostics_snapshot()[-1]
    assert entry["stage"] == "mint_upload"
    assert entry["http_status"] == 502
    assert entry["arena_status"] == 403
    assert entry["source"] == "drop"
    rendered = "\n".join(web._upload_diagnostic_lines())
    assert "private-name" not in rendered
    assert "sig=secret" not in rendered
    assert "storage.example" not in rendered


def test_upload_diagnostics_ring_is_bounded():
    web._UPLOAD_DIAGNOSTICS.clear()
    for index in range(25):
        web._record_upload_diagnostic(
            {"id": f"id{index}", "kind": "evaluation"},
            stage="validate", outcome="error", http_status=400,
            code="unsupported_attachment")
    entries = web._upload_diagnostics_snapshot()
    assert len(entries) == web._UPLOAD_DIAGNOSTICS_MAX == 20
    assert entries[0]["id"] == "id5"
    assert entries[-1]["id"] == "id24"


def test_model_input_support_treats_tag_absence_as_unknown():
    from arena import models

    assert models.model_input_support({"capabilities": ["Router"]}) == {
        "image": None, "file": None}
    assert models.model_input_support({"capabilities": ["Vision", "PDF"]}) == {
        "image": True, "file": True}
    assert models.model_input_support({
        "capabilities": {"inputCapabilities": {"text": {}, "image": False}}
    }) == {"image": False, "file": False}
