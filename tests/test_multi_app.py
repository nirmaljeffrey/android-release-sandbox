"""Multi-app config: several apps on several Play developer accounts."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from release_bot import cli, config

HERE = Path(__file__).resolve().parent
MULTI = HERE / "release-bot.multi.test.yml"
RAW = config.load(MULTI)


def test_apps_resolve_with_their_account_environment():
    shop = config.resolve(RAW, "shop")
    partner = config.resolve(RAW, "partner-app")
    assert (shop["account"], shop["environment"]) == ("vaazh", "play-vaazh")
    assert (partner["account"], partner["environment"]) == ("partner", "play-partner")
    assert config.resolve(RAW, "chat")["environment"] == "play-vaazh"


def test_app_overrides_merge_over_defaults():
    chat = config.resolve(RAW, "chat")
    assert chat["health"]["min_distinct_users"] == 200
    assert chat["health"]["play_vitals"]["halt"]["user_perceived_anr_rate"] == 0.0047  # still inherited
    fitness = config.resolve(RAW, "fitness")
    assert fitness["rollout"]["targets"] == {"tuesday": 0.20, "wednesday": 0.50, "thursday": 1.0}
    assert fitness["rollout"]["steps"] == [0.02, 0.20, 0.50, 1.0]
    partner = config.resolve(RAW, "partner-app")
    assert partner["slack"]["channel_id"] == "C_PARTNER"
    assert partner["slack"]["alerts_channel_id"] == "C_ALERTS"


def test_names_builds_tags_and_repos():
    shop = config.resolve(RAW, "shop")
    assert shop["display_name"] == "Shop Android"
    assert config.resolve(RAW, "partner-app")["display_name"] == "Partner Android"
    assert config.resolve(RAW, "fitness")["display_name"] == "Fitness Android"
    assert shop["build"]["project_dir"] == "apps/shop"
    assert shop["build"]["bundle_task"] == ":app:bundleRelease"
    assert shop["signing_environment"] == "android-signing-shop"
    assert config.resolve(RAW, "chat")["signing_environment"] == "android-signing"
    assert config.version_from_tag(shop, "shop/v4.12.0") == "4.12.0"
    with pytest.raises(config.ConfigError):
        config.version_from_tag(shop, "chat/v4.12.0")
    assert config.resolve(RAW, "partner-app")["repository"] == "partner-org/partner-android"
    assert config.version_from_tag(config.resolve(RAW, "partner-app"), "v2.0.1") == "2.0.1"


def test_errors_are_explicit():
    with pytest.raises(config.ConfigError, match="pass --app"):
        config.resolve(RAW, None)
    with pytest.raises(config.ConfigError, match="unknown app"):
        config.resolve(RAW, "nope")
    bad = json.loads(json.dumps(RAW))
    bad["apps"]["shop"]["account"] = "missing"
    with pytest.raises(config.ConfigError, match="isn't under accounts"):
        config.resolve(bad, "shop")


def test_legacy_single_app_config_still_resolves():
    legacy = config.resolve(config.load(HERE / "release-bot.test.yml"))
    assert legacy["app_id"] == "app" and legacy["environment"] == "play-production"
    assert legacy["display_name"] == "Android"


def test_apps_command_emits_matrix(capsys):
    assert cli.main(["--config", str(MULTI), "apps"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {"app": "shop", "environment": "play-vaazh", "account": "vaazh"},
        {"app": "chat", "environment": "play-vaazh", "account": "vaazh"},
        {"app": "fitness", "environment": "play-vaazh", "account": "vaazh"},
        {"app": "partner-app", "environment": "play-partner", "account": "partner"},
    ]


def test_app_info_outputs_and_tag_validation(capsys):
    assert cli.main(["--config", str(MULTI), "--app", "shop", "app-info", "--tag", "shop/v1.4.0"]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.strip().splitlines())
    assert out["environment"] == "play-vaazh" and out["version"] == "1.4.0"
    assert out["signing_environment"] == "android-signing-shop" and out["project_dir"] == "apps/shop"
    assert cli.main(["--config", str(MULTI), "--app", "shop", "app-info", "--tag", "v1.4.0"]) != 0
    assert cli.main(["--config", str(MULTI), "app-info"]) == 2   # several apps, no --app


# ---- mock mode: apps roll out independently ----

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("RELEASE_BOT_MOCK", "true")
    monkeypatch.delenv("MOCK_STATE_FILE", raising=False)
    monkeypatch.chdir(tmp_path)                # mock state goes to ./.mock-state/<app>.json
    monkeypatch.setenv("MOCK_ON_DUTY", "hero")
    monkeypatch.setenv("MOCK_REVIEW_MINUTES", "0")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    clock = {"now": datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(cli, "utcnow", lambda: clock["now"])

    def run(app, *argv):
        return cli.main(["--config", str(MULTI), "--app", app, *argv])
    run.state = lambda app: json.loads((tmp_path / ".mock-state" / f"{app}.json").read_text())
    run.tick = lambda: clock.__setitem__("now", clock["now"] + timedelta(minutes=1))
    return run


def live(state):
    return next((r for r in state["releases"] if r["status"] in ("inProgress", "halted")), None)


def test_two_accounts_roll_out_independently(sandbox, capsys):
    sandbox("shop", "submit", "--aab", "x.aab", "--version", "4.12.0")
    sandbox("partner-app", "submit", "--aab", "y.aab", "--version", "2.0.1")
    sandbox.tick()
    sandbox("shop", "advance")
    sandbox("partner-app", "advance")
    assert live(sandbox.state("shop"))["userFraction"] == 0.20
    assert live(sandbox.state("partner-app"))["userFraction"] == 0.20

    sandbox("partner-app", "mock-inject", "--incident", "new-crash")
    sandbox("partner-app", "check")
    sandbox("shop", "check")
    assert live(sandbox.state("partner-app"))["status"] == "halted"
    assert live(sandbox.state("shop"))["status"] == "inProgress"    # unaffected

    out = capsys.readouterr().out
    assert "Shop Android *4.12.0*" in out and "Partner Android" in out
    assert "partner-app 2.0.1:" in out                               # separate Slack thread key
