from datetime import datetime, timezone
from pathlib import Path

import pytest

from release_bot import cli, config, gate, schedule, tags
from release_bot.gate import Level
from release_bot.play import TrackState

CFG = config.load(Path(__file__).resolve().parent / "release-bot.test.yml")
STEPS = CFG["rollout"]["steps"]
TARGETS = CFG["rollout"]["targets"]
TZ = CFG["timezone"]


def at(day: str) -> datetime:
    # Week of 2026-10-05: Mon 5 … Sun 11, 08:00 UTC
    days = {"mon": 5, "tue": 6, "wed": 7, "thu": 8, "fri": 9, "sat": 10, "sun": 11}
    return datetime(2026, 10, days[day], 8, 0, tzinfo=timezone.utc)


# ---------- schedule ----------

@pytest.mark.parametrize("day,current,expected", [
    ("mon", 0.02, 0.20),
    ("tue", 0.20, 0.50),
    ("wed", 0.50, 1.0),
    ("tue", 0.02, 0.20),   # Monday was held → only one step, not straight to 50%
    ("mon", 0.20, None),   # already at today's target
    ("thu", 0.50, None),   # not a rollout day
    ("fri", 0.02, None),
    ("sat", 0.02, None),
])
def test_next_fraction_follows_weekly_plan(day, current, expected):
    target = schedule.target_for(at(day), TARGETS, TZ)
    assert schedule.next_fraction(current, STEPS, target) == expected


# ---------- tags ----------

def test_previous_tag_and_version_name():
    assert tags.previous_tag("v4.12.0", ["v4.12.0", "v4.11.1", "v4.11.0"]) == "v4.11.1"
    assert tags.previous_tag("v1.0.0", ["v1.0.0"]) is None
    assert tags.version_name("v4.12.0") == "4.12.0"
    with pytest.raises(ValueError):
        tags.previous_tag("v9.9.9", ["v1.0.0"])


# ---------- gate ----------

VCFG = CFG["health"]["play_vitals"]


def vit(users=50_000, crash=0.004, anr=0.002):
    return {"distinctUsers": users, "userPerceivedCrashRate": crash, "userPerceivedAnrRate": anr}


def test_vitals_healthy():
    assert max(f.level for f in gate.evaluate_vitals(vit(), vit(), VCFG, 1000)) == Level.OK


def test_vitals_not_enough_users():
    [f] = gate.evaluate_vitals(vit(users=200), vit(), VCFG, 1000)
    assert f.level == Level.NOT_ENOUGH_DATA


def test_vitals_missing_version_means_not_enough_data():
    [f] = gate.evaluate_vitals(None, vit(), VCFG, 1000)
    assert f.level == Level.NOT_ENOUGH_DATA


def test_vitals_google_anr_threshold_halts():
    levels = [f.level for f in gate.evaluate_vitals(vit(anr=0.0050), vit(), VCFG, 1000)]
    assert Level.HALT in levels


def test_vitals_relative_regression_holds():
    findings = gate.evaluate_vitals(vit(crash=0.006), vit(crash=0.004), VCFG, 1000)  # 1.5× previous
    assert max(f.level for f in findings) == Level.HOLD


def test_vitals_first_release_without_baseline_is_ok():
    assert max(f.level for f in gate.evaluate_vitals(vit(), None, VCFG, 1000)) == Level.OK


def test_crashlytics_new_issue_halts():
    [f] = gate.evaluate_crashlytics([{"title": "NPE in Checkout", "users": 40}], {})
    assert f.level == Level.HALT and "Checkout" in f.message


def test_grafana_severity_mapping():
    gcfg = CFG["health"]["grafana"]
    alerts = [
        {"labels": {"alertname": "API 5xx", "severity": "critical"}},
        {"labels": {"alertname": "Latency", "severity": "warning"}},
        {"labels": {"alertname": "Disk", "severity": "info"}},
    ]
    levels = sorted(f.level for f in gate.evaluate_grafana(alerts, gcfg))
    assert levels == [Level.HOLD, Level.HALT]
    assert gate.evaluate_grafana([], gcfg)[0].level == Level.OK


# ---------- track parsing ----------

def test_track_state_live_and_previous():
    state = TrackState([
        {"name": "4.11.0", "versionCodes": ["41100"], "status": "completed"},
        {"name": "4.12.0", "versionCodes": ["41200"], "status": "inProgress", "userFraction": 0.2},
    ])
    assert state.live["name"] == "4.12.0"
    assert TrackState.version_code(state.completed) == 41100


# ---------- command flows with fakes ----------

class FakePlay:
    def __init__(self, releases):
        self.releases = releases
        self.actions = []

    def track_state(self):
        return TrackState(self.releases)

    def _live(self):
        return TrackState(self.releases).live

    def set_fraction(self, f):
        self.actions.append(("set_fraction", f))
        return self._live()

    def halt(self):
        self.actions.append(("halt",))
        return self._live()

    def resume(self):
        self.actions.append(("resume",))
        return self._live()

    def upload_and_start(self, *a):
        self.actions.append(("upload", *a))
        return 41200


class FakeSlack:
    def __init__(self, heroes=()):
        self.messages = []
        self.channel_posts = []
        self.heroes = set(heroes)

    def post(self, version, text, root_text=None):
        self.messages.append(text)

    def announce(self, channel, text):
        if channel:
            self.channel_posts.append((channel, text))

    def usergroup_members(self, group):
        return self.heroes

    def thread_contains(self, version, text):
        return any(text in m for m in self.messages)


class Src:
    def __init__(self, vitals=None, issues=(), alerts=(), error=None):
        self._v, self._i, self._a, self._e = vitals, list(issues), list(alerts), error

    def latest_by_version(self):
        if self._e:
            raise self._e
        return self._v or {}

    def new_fatal_issues(self, code):
        return self._i

    def active_alerts(self):
        return self._a


ROLLING = [
    {"name": "4.11.0", "versionCodes": ["41100"], "status": "completed"},
    {"name": "4.12.0", "versionCodes": ["41200"], "status": "inProgress", "userFraction": 0.02},
]


def make_deps(releases=ROLLING, vitals=None, issues=(), alerts=(), vitals_error=None):
    src = Src(vitals if vitals is not None else {41200: vit(), 41100: vit()}, issues, alerts, vitals_error)
    return cli.Deps(cfg=CFG, play=FakePlay([dict(r) for r in releases]), slack=FakeSlack(),
                    vitals=src, crashlytics=src, grafana=src)


@pytest.fixture
def monday(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return at("mon")
    monkeypatch.setattr(cli, "datetime", FixedDatetime)


def test_advance_healthy_moves_one_step(monday):
    deps = make_deps()
    cli.main(["advance"], deps)
    assert deps.play.actions == [("set_fraction", 0.20)]
    assert "2% → 20%" in deps.slack.messages[0]


def test_advance_with_new_crash_halts_instead(monday):
    deps = make_deps(issues=[{"title": "Crash on launch", "users": 300}])
    cli.main(["advance"], deps)
    assert deps.play.actions == [("halt",)]


def test_advance_holds_when_not_enough_users(monday):
    deps = make_deps(vitals={41200: vit(users=10)})
    cli.main(["advance"], deps)
    assert deps.play.actions == []
    assert "Holding at 2%" in deps.slack.messages[0]


def test_advance_holds_when_a_source_is_down(monday):
    deps = make_deps(vitals_error=RuntimeError("503"))
    cli.main(["advance"], deps)
    assert deps.play.actions == []


def test_advance_never_touches_a_halted_release(monday):
    halted = [ROLLING[0], {**ROLLING[1], "status": "halted"}]
    deps = make_deps(releases=halted)
    cli.main(["advance"], deps)
    assert deps.play.actions == []
    assert "Still halted" in deps.slack.messages[0]


def test_check_halts_on_critical_grafana_alert():
    deps = make_deps(alerts=[{"labels": {"alertname": "API 5xx", "severity": "critical"}}])
    cli.main(["check", "--trigger", "grafana webhook"], deps)
    assert deps.play.actions == [("halt",)]
    assert "grafana webhook" in deps.slack.messages[0]


def test_check_source_outage_warns_but_does_not_halt():
    deps = make_deps(vitals_error=RuntimeError("timeout"))
    cli.main(["check"], deps)
    assert deps.play.actions == []
    assert "needs a human look" in deps.slack.messages[0]


def test_check_quiet_when_healthy():
    deps = make_deps()
    cli.main(["check"], deps)
    assert deps.play.actions == [] and deps.slack.messages == []


def test_precheck_blocks_while_previous_rollout_unfinished():
    deps = make_deps()
    assert cli.main(["precheck-submit"], deps) == 1
    assert cli.main(["precheck-submit", "--force"], deps) == 0


def test_precheck_allows_when_previous_is_complete():
    deps = make_deps(releases=[ROLLING[0]])
    assert cli.main(["precheck-submit"], deps) == 0


def test_submit_uses_default_notes_and_initial_fraction():
    deps = make_deps(releases=[ROLLING[0]])
    cli.main(["submit", "--aab", "app.aab", "--version", "4.12.0"], deps)
    _, aab, version, fraction, notes, lang = deps.play.actions[0]
    assert (aab, version, fraction, lang) == ("app.aab", "4.12.0", 0.02, "en-US")
    assert notes == CFG["play"]["default_release_notes"]


ANNOUNCE = CFG["slack"]["announce_channel_id"]
ALERTS = CFG["slack"]["alerts_channel_id"]


def test_auto_halt_posts_to_slo_alerts_and_wider_group():
    deps = make_deps(alerts=[{"labels": {"alertname": "API 5xx", "severity": "critical"}}])
    cli.main(["check"], deps)
    channels = [c for c, _ in deps.slack.channel_posts]
    assert ALERTS in channels and ANNOUNCE in channels
    assert f"<!subteam^{CFG['slack']['hero_usergroup_id']}>" in deps.slack.messages[0]


def test_full_release_is_announced(monkeypatch):
    class Wed(datetime):
        @classmethod
        def now(cls, tz=None):
            return at("wed")
    monkeypatch.setattr(cli, "datetime", Wed)
    deps = make_deps(releases=[ROLLING[0], {**ROLLING[1], "userFraction": 0.5}])
    cli.main(["advance"], deps)
    assert deps.play.actions == [("set_fraction", 1.0)]
    assert any("fully released" in t for c, t in deps.slack.channel_posts if c == ANNOUNCE)


def test_submit_announces_to_wider_group():
    deps = make_deps(releases=[ROLLING[0]])
    cli.main(["submit", "--aab", "app.aab", "--version", "4.12.0"], deps)
    assert any("4.12.0" in t for c, t in deps.slack.channel_posts if c == ANNOUNCE)


def test_authorize_only_on_duty_hero():
    gh_login, slack_id = next(iter(CFG["access"]["release_heroes"].items()))
    deps = make_deps()
    deps.slack = FakeSlack(heroes={slack_id})
    assert cli.main(["authorize", "--actor", gh_login], deps) == 0

    deps.slack = FakeSlack(heroes={"USOMEONEELSE"})      # rotated to someone else
    assert cli.main(["authorize", "--actor", gh_login], deps) == 1

    deps.slack = FakeSlack(heroes={slack_id})
    assert cli.main(["authorize", "--actor", "not-mapped"], deps) == 1


def test_check_does_not_repeat_the_same_warning():
    deps = make_deps(vitals_error=RuntimeError("timeout"))
    cli.main(["check"], deps)
    cli.main(["check"], deps)
    assert len(deps.slack.messages) == 1
