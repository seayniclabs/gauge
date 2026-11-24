"""Keep tests away from the user's real files: the approval log and config file go to a temp folder."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    # Tests build Config(env={...}) without these, so the paths fall back to Path.home(): point HOME away too.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GAUGE_APPROVAL_LOG", str(tmp_path / "approvals.jsonl"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("GAUGE_CONFIG", str(tmp_path / "no-config.toml"))
