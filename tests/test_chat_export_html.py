"""Standalone HTML chat export must be styled but never trust chat markup."""

from arena.web import app as web


def _data():
    return {
        "title": "<script>alert('title')</script>",
        "mode": "direct",
        "modality": "text",
        "messages": [
            {"role": "user", "text": "Look at <img src=x onerror=alert(1)>"},
            {
                "role": "assistant",
                "text": (
                    "# Result\n\n"
                    "A [safe link](https://example.com) and a "
                    "[bad link](javascript:alert(1)).\n\n"
                    "```python\nprint('<hello>')\n```\n\n"
                    "| Name | Value |\n| --- | --- |\n| **One** | `1` |\n\n"
                    "![Remote](https://example.com/image.png)\n\n"
                    "![Embedded](data:image/png;base64,iVBORw0KGgo=)"
                ),
                "reasoning": "Private <thought>",
                "names": {"a": "Model <One>"},
                "tools": [{"tool": "read_file", "summary": "Read <file>",
                           "status": "completed", "lane": "a"}],
            },
        ],
    }


def test_html_export_escapes_content_and_renders_safe_markdown():
    exported = web._chat_export_html(_data())
    assert "<script>" not in exported
    assert "&lt;script&gt;" in exported
    assert "onerror=alert(1)&gt;" in exported
    assert 'href="https://example.com"' in exported
    assert "javascript:alert" not in exported
    assert "<table>" in exported and "<strong>One</strong>" in exported
    assert "<pre><code" in exported and "&lt;hello&gt;" in exported
    assert "Reflections" in exported and "Private &lt;thought&gt;" in exported
    assert "Tool steps (1)" in exported and "Read &lt;file&gt;" in exported
    assert 'href="https://example.com/image.png"' in exported
    assert 'src="https://example.com/image.png"' not in exported
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in exported


def test_html_export_route_downloads_attachment(monkeypatch):
    monkeypatch.setattr(web, "_load_chat", lambda _chat_id: _data())
    response = web.app.test_client().get("/api/chats/example/export.html")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "attachment;" in response.headers["Content-Disposition"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
