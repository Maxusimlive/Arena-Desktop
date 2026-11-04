"""Workspace Git panel: real temporary repository, list-argument Git calls."""

import subprocess

import pytest

from arena.web import app as web


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def git_client(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Arena Test")
    (root / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "initial")
    monkeypatch.setattr(web, "_panel_workspace_root", lambda: root.resolve())
    return web.app.test_client(), root


def test_git_status_stage_diff_commit_and_unstage(git_client):
    client, root = git_client
    (root / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (root / "new.txt").write_text("new file\n", encoding="utf-8")

    status = client.get("/api/workspace/git/status").get_json()
    assert status["ok"] is True and status["clean"] is False
    by_path = {item["path"]: item for item in status["files"]}
    assert by_path["tracked.txt"]["unstaged"] is True
    assert by_path["new.txt"]["untracked"] is True
    assert status["recent_commits"][0]["subject"] == "initial"

    staged = client.post("/api/workspace/git/stage",
                         json={"paths": ["tracked.txt", "new.txt"]}).get_json()
    assert staged["staged_count"] == 2
    diff = client.get("/api/workspace/git/diff", query_string={
        "path": "tracked.txt", "staged": "1"}).get_json()
    assert "+two" in diff["diff"]

    unstaged = client.post("/api/workspace/git/unstage",
                           json={"paths": ["new.txt"]}).get_json()
    assert unstaged["staged_count"] == 1
    assert {item["path"]: item for item in unstaged["files"]}["new.txt"]["untracked"] is True

    committed = client.post("/api/workspace/git/commit",
                            json={"message": "update tracked file"})
    assert committed.status_code == 200, committed.get_json()
    payload = committed.get_json()
    assert payload["committed"]["subject"] == "update tracked file"
    assert [item["path"] for item in payload["files"]] == ["new.txt"]


def test_git_panel_rejects_paths_outside_workspace(git_client):
    client, _root = git_client
    resp = client.post("/api/workspace/git/stage", json={"paths": ["../outside.txt"]})
    assert resp.status_code == 403


def test_git_panel_requires_workspace_to_be_repo_root(git_client, monkeypatch):
    client, root = git_client
    subdir = root / "inside"
    subdir.mkdir()
    monkeypatch.setattr(web, "_panel_workspace_root", lambda: subdir.resolve())
    resp = client.get("/api/workspace/git/status")
    assert resp.status_code == 400
    assert "repository root" in resp.get_json()["error"]
