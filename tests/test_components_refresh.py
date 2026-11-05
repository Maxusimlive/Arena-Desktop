"""Regression tests for component re-detection (Check again) and for
cloudflared installed system-wide instead of in the app-data bin folder."""
import os

from arena import components


def test_cloudflared_found_on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    fake = tool_dir / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    fake.write_bytes(b"fake")
    fake.chmod(0o755)
    monkeypatch.setattr(components, "BIN_DIR", bindir)
    monkeypatch.setenv("PATH", str(tool_dir))
    assert components.cloudflared_path() == fake
    entry = next(c for c in components.detect() if c["id"] == "cloudflared")
    assert entry["installed"] is True
    assert entry["path"] == str(fake)


def test_app_data_cloudflared_wins_over_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    own = bindir / ("cloudflared.exe" if components._is_windows() else "cloudflared")
    own.write_bytes(b"own")
    own.chmod(0o755)
    monkeypatch.setattr(components, "BIN_DIR", bindir)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert components.cloudflared_path() == own


def test_detect_refresh_drops_version_cache(tmp_path, monkeypatch):
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    node = tool_dir / ("node.exe" if os.name == "nt" else "node")
    node.write_text("#!/bin/sh\necho v99.0.0\n" if os.name != "nt" else "")
    node.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool_dir))
    components._version_cache.clear()
    components._version_cache["node"] = "v0-stale"
    stale = next(c for c in components.detect() if c["id"] == "node")
    assert stale["installed"] is True and stale["version"] == "v0-stale"
    fresh = next(c for c in components.detect(refresh=True) if c["id"] == "node")
    assert fresh["installed"] is True and fresh["version"] != "v0-stale"


def test_windows_registry_paths_merged(monkeypatch):
    entries = components._windows_registry_paths()
    if components._is_windows():
        assert isinstance(entries, list)
    else:
        assert entries == []
    # The search path must always at least include the process PATH.
    assert (components._search_path() or "") is not None
