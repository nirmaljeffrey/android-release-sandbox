"""User-configurable rollout schedules (per team, per platform) and health rules."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from release_bot import cli, config, policy, rules
from release_bot.gate import Level, Verdict

HERE = Path(__file__).resolve().parent
TEAMS = HERE / "release-bot.teams.test.yml"
RAW = config.load(TEAMS)


def at(day: int, hour: int = 8) -> datetime:      # Oct 2026: Mon 5 … Sun 11, Mon 12 …
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


# ---------------- schedules ----------------

def test_weekly_train_mon_1_tue_2_wed_100():
    sched = policy.from_config(config.resolve(RAW, "checkout-team")).android
    assert sched.initial == 0.01 and sched.ladder == [0.01, 0.02, 1.0]
    assert sched.target(at(5), "UTC") == 0.01               # Monday
    assert sched.next_fraction(0.01, sched.target(at(6), "UTC")) == 0.02   # Tuesday
    assert sched.next_fraction(0.02, sched.target(at(7), "UTC")) == 1.0    # Wednesday
    assert sched.target(at(8), "UTC") is None               # Thursday: nothing due
    assert sched.summary() == "1% Mon → 2% Tue → 100% Wed"


def test_day_n_schedule_counts_from_submit_day():
    pol = policy.from_config(config.resolve(RAW, "growth-team"))
    android = pol.android
    submitted = at(8, 18)                                    # Thursday evening
    assert android.target(at(9), "UTC", submitted) == 0.05   # day 1
    assert android.target(at(10), "UTC", submitted) == 0.05  # day 2: still 5%
    assert android.target(at(11), "UTC", submitted) == 0.25  # day 3
    assert android.target(at(13), "UTC", submitted) == 1.0   # day 5
    assert android.target(at(13), "UTC", None) is None       # unknown submit time → wait
    assert pol.when_data_is_thin == "advance"


def test_aligned_schedule_matches_apples_phased_release():
    pol = policy.from_config(config.resolve(RAW, "checkout-team"))
    errors, warnings = policy.check(pol, ["android", "ios"])
    assert errors == []
    assert any("Android" in w and "approves" in w for w in warnings)


def test_ios_rejects_what_apple_cant_do():
    pol = policy.from_config(config.resolve(RAW, "bad-ios-team"))
    errors, _ = policy.check(pol, ["android", "ios"])
    assert len(errors) == 1 and "phased-release day 1" in errors[0] and "Use 1%" in errors[0]
    assert policy.check(pol, ["android"])[0] == []           # fine for an Android-only app


def test_separate_ios_schedule_with_gaps_is_checked_against_apple_days():
    pol = policy.from_config(config.resolve(RAW, "growth-team"))
    assert policy.check(pol, ["ios"])[0] == []               # day 4 → 1%, day 6 → 5% (phased day 3), day 7 → 100%


def test_weekday_gaps_wrap_across_the_weekend():
    s = policy._schedule_from([{"on": "friday", "percent": 1}, {"on": "monday", "percent": 5}], "x")
    assert s.day_offsets() == [0, 3]                         # Fri→Mon = phased day 4 (10%), so 5% is wrong
    assert policy._check_ios(s, "separate")


@pytest.mark.parametrize("bad, msg", [
    ([{"on": "monday", "percent": 5}, {"on": "tuesday", "percent": 2}], "must increase"),
    ([{"on": "monday", "percent": 5}, {"on": "day 3", "percent": 10}], "not both"),
    ([{"on": "someday", "percent": 5}], "weekday"),
    ([{"on": "monday", "percent": 150}], "between 0 and 100"),
    ([], "at least one step"),
    ([{"on": "monday", "percent": 1}, {"on": "wednesday", "percent": 5},
      {"on": "friday", "percent": 20}, {"on": "tuesday", "percent": 100}], "within one week"),
    ([{"on": "monday", "percent": 1}, {"on": "monday", "percent": 5}], "once"),
])
def test_schedule_validation(bad, msg):
    with pytest.raises(policy.PolicyError, match=msg):
        policy._schedule_from(bad, "rollout.schedule")


def test_legacy_steps_and_targets_still_work():
    legacy = config.resolve(config.load(HERE / "release-bot.test.yml"))
    sched = policy.from_config(legacy).android
    assert sched.initial == 0.02 and sched.ladder == [0.02, 0.2, 0.5, 1.0]
    assert sched.next_fraction(0.02, sched.target(at(6), "Europe/Berlin")) == 0.2   # Tuesday: one step only


# ---------------- health rules ----------------

def norm(app):
    return rules.normalize(config.resolve(RAW, app)["health"])


def test_rules_parse_percent_strings_and_actions():
    n = norm("checkout-team")
    anr = next(r for r in n["rules"] if r.name == "Google ANR line")
    assert anr.above == pytest.approx(0.0047) and anr.level == Level.HALT
    reg = next(r for r in n["rules"] if r.name == "ANR regression")
    assert reg.above_previous_by == pytest.approx(0.25) and reg.level == Level.HOLD
    assert set(n["sources"]) == {"play_vitals", "crashlytics", "grafana"}


def test_each_team_has_its_own_rules():
    growth = norm("growth-team")
    assert [r.name for r in growth["rules"]] == ["Strict ANR", "Any new crash"]
    assert growth["min_users"] == 200
    vit = {"distinctUsers": 500, "userPerceivedAnrRate": 0.0035, "userPerceivedCrashRate": 0.004}
    # 0.35% ANR: halts growth (0.30% line) but not checkout (0.47% line, 1.4× is a hold there)
    g = rules.evaluate(growth, {"play_vitals": {"new": vit, "prev": {"userPerceivedAnrRate": 0.0025}}})
    c = rules.evaluate(norm("checkout-team"), {"play_vitals": {"new": {**vit, "distinctUsers": 5000},
                                                               "prev": {"userPerceivedAnrRate": 0.0025}}})
    assert Verdict(g).level == Level.HALT
    assert Verdict(c).level == Level.HOLD


def test_notify_rule_tells_but_does_not_block():
    alerts = [{"labels": {"alertname": "p95 latency high", "severity": "warning"}}]
    findings = rules.evaluate(norm("checkout-team"), {"grafana": {"alerts": alerts}})
    assert Verdict(findings).level == Level.NOTIFY
    other = [{"labels": {"alertname": "disk almost full", "severity": "warning"}}]   # no rule matches the name
    assert Verdict(rules.evaluate(norm("checkout-team"), {"grafana": {"alerts": other}})).level == Level.OK


@pytest.mark.parametrize("bad, msg", [
    ({"source": "play_vitals", "metric": "user_perceived_anr_rate", "action": "halt"}, "exactly one"),
    ({"source": "play_vitals", "metric": "battery", "above": "1%"}, "metric must be"),
    ({"source": "datadog", "action": "halt"}, "source must be"),
    ({"source": "grafana", "action": "halt"}, "severity"),
    ({"source": "crashlytics", "action": "explode"}, "action must be"),
])
def test_rule_validation(bad, msg):
    with pytest.raises(rules.RuleError, match=msg):
        rules.parse_rule(bad, 1)


# ---------------- CLI: plan / validate / advance ----------------

def test_validate_flags_only_the_bad_team(capsys):
    assert cli.main(["--config", str(TEAMS), "validate"]) == 1
    out = capsys.readouterr().out
    assert "bad-ios-team" in out and "checkout-team: " not in out.replace("::warning::checkout-team", "")
    assert cli.main(["--config", str(TEAMS), "--app", "checkout-team", "validate"]) == 0


def test_plan_shows_both_platforms_and_rules(capsys):
    cli.main(["--config", str(TEAMS), "--app", "checkout-team", "plan"])
    out = capsys.readouterr().out
    assert "Android  Mon" in out and "iOS      Mon" in out
    assert "Apple's phased release, day 2" in out and "releases to everyone" in out
    assert "halt   play_vitals:user_perceived_anr_rate ≥ 0.47%" in out
    assert "notify grafana:alert severity warning, alert ~ /latency/" in out


class FakePlay:
    def __init__(self, fraction):
        self.releases = [
            {"name": "1.0.0", "versionCodes": ["100"], "status": "completed"},
            {"name": "1.1.0", "versionCodes": ["110"], "status": "inProgress", "userFraction": fraction},
        ]
        self.actions = []

    def track_state(self):
        from release_bot.play import TrackState
        return TrackState(self.releases)

    def set_fraction(self, f):
        self.actions.append(f)
        return self.releases[1]

    def halt(self):
        self.actions.append("halt")
        return self.releases[1]


class Slack:
    def __init__(self):
        self.posts, self.channels = [], []

    def post(self, key, text, root_text=None):
        self.posts.append(text)

    def announce(self, channel, text):
        self.channels.append((channel, text))

    def thread_contains(self, key, text):
        return False


class Vitals:
    def __init__(self, users=5000, anr=0.002):
        self.users, self.anr = users, anr

    def latest_by_version(self):
        return {110: {"distinctUsers": self.users, "userPerceivedAnrRate": self.anr, "userPerceivedCrashRate": 0.004},
                100: {"distinctUsers": 90000, "userPerceivedAnrRate": 0.002, "userPerceivedCrashRate": 0.004}}


class Quiet:
    def new_fatal_issues(self, code):
        return []

    def active_alerts(self):
        return [{"labels": {"alertname": "p95 latency high", "severity": "warning"}}]


def advance(app, fraction, when, vitals=None):
    cfg = config.resolve(RAW, app)
    deps = cli.Deps(cfg=cfg, play=FakePlay(fraction), slack=Slack(), vitals=vitals or Vitals(),
                    crashlytics=Quiet(), grafana=Quiet())
    orig = cli.utcnow
    cli.utcnow = lambda: when
    try:
        cli.cmd_advance(deps, None)
    finally:
        cli.utcnow = orig
    return deps


def test_advance_follows_team_schedule_and_notifies():
    d = advance("checkout-team", 0.01, at(6))                # Tuesday
    assert d.play.actions == [0.02]
    assert any("FYI" in t for _, t in d.slack.channels)      # latency warning → notify, still advanced
    assert advance("checkout-team", 0.01, at(5)).play.actions == []   # Monday: already at 1%


def test_thin_data_advances_only_when_team_allows_it(monkeypatch):
    thin = Vitals(users=50)
    assert advance("checkout-team", 0.01, at(6), thin).play.actions == []        # hold (default)
    monkeypatch.setattr(cli, "_submitted_at", lambda *a: at(8, 18))
    assert advance("growth-team", 0.05, at(11), thin).play.actions == [0.25]     # when_data_is_thin: advance
