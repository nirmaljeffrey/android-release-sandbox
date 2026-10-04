"""Mock mode drives the real CLI end to end with a JSON state file (what the sandbox repo runs)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from release_bot import cli

MONDAY = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("RELEASE_BOT_MOCK", "true")
    monkeypatch.setenv("MOCK_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("MOCK_ON_DUTY", "hero")
    monkeypatch.setenv("MOCK_REVIEW_MINUTES", "5")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    clock = {"now": MONDAY}
    monkeypatch.setattr(cli, "utcnow", lambda: clock["now"])

    config = str(Path(__file__).resolve().parent / "release-bot.test.yml")

    def run(*argv):
        return cli.main(["--config", config, *argv])

    def advance_time(**kw):
        clock["now"] += timedelta(**kw)

    run.advance_time = advance_time
    run.state = tmp_path / "state.json"
    return run


def live(run):
    import json
    data = json.loads(run.state.read_text())
    return next((r for r in data["releases"] if r["status"] in ("inProgress", "halted")), None), data


def test_full_week_in_mock_mode(sandbox):
    assert sandbox("authorize", "--actor", "hero") == 0
    assert sandbox("authorize", "--actor", "intruder") == 1
    assert sandbox("precheck-submit") == 0
    assert sandbox("submit", "--aab", "x.aab", "--version", "4.12.0") == 0
    r, _ = live(sandbox)
    assert (r["status"], r["userFraction"], r["versionCodes"]) == ("inProgress", 0.02, ["41200"])

    sandbox("advance")                      # still in mock Google review → hold
    assert live(sandbox)[0]["userFraction"] == 0.02

    sandbox.advance_time(minutes=10)        # review done, 2% live
    sandbox("advance")
    assert live(sandbox)[0]["userFraction"] == 0.20

    sandbox.advance_time(days=1)
    sandbox("advance")
    assert live(sandbox)[0]["userFraction"] == 0.50

    sandbox("mock-inject", "--incident", "new-crash")
    sandbox("check")
    r, data = live(sandbox)
    assert r["status"] == "halted"

    sandbox("mock-inject", "--incident", "none")
    assert sandbox("resume", "--reason", "false alarm") == 0
    sandbox.advance_time(days=1)
    sandbox("advance")
    r, data = live(sandbox)
    assert r is None and data["releases"][0]["status"] == "completed"


def test_precheck_blocks_second_submit_while_rolling_out(sandbox):
    sandbox("submit", "--aab", "x.aab", "--version", "4.12.0")
    assert sandbox("precheck-submit") == 1


def test_mock_play_rejects_non_increasing_version(sandbox):
    sandbox("submit", "--aab", "x.aab", "--version", "4.12.0")
    sandbox.advance_time(minutes=10)
    for _ in range(3):
        sandbox("advance"); sandbox.advance_time(days=1)
    with pytest.raises(RuntimeError, match="must be higher"):
        sandbox("submit", "--aab", "x.aab", "--version", "4.12.0")
