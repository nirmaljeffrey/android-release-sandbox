"""Replay one release week (Thu → Wed) against mock data, using the real bot.

Every event is what GitHub Actions would run in production:
  Thu 18:00          Android · Submit to Play   (authorize → precheck → submit)
  every 3h at :17    Android · Health check
  Mon–Wed 07:00      Android · Rollout step
  alert webhooks     Android · Health check (repository_dispatch)
  manual actions     Android · HALT / Resume
"""

import contextlib
import copy
import io
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from release_bot import cli
from release_bot import config as config_mod
from sim.fakes import Clock, FakeCrashlytics, FakeGrafana, FakePlay, FakeVitals, RecordingSlack

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = Path(__file__).parent / "scenarios"

# A fixed week: Thu 8 Oct → Wed 14 Oct 2026 (UTC).
WEEK = {d: date(2026, 10, 8) + timedelta(days=i)
        for i, d in enumerate(["thu", "fri", "sat", "sun", "mon", "tue", "wed"])}
END = "wed 12:00"


def parse_time(s: str) -> datetime:
    day, hhmm = s.split()
    h, m = map(int, hhmm.split(":"))
    d = WEEK[day.lower()[:3]]
    return datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc)


@dataclass
class Run:
    at: datetime
    workflow: str
    trigger: str
    ok: bool
    output: str


@dataclass
class Result:
    scenario: dict
    runs: list[Run] = field(default_factory=list)
    slack: RecordingSlack | None = None
    play: FakePlay | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def final(self) -> dict:
        live = self.play.track_state().live
        if live and int(live["versionCodes"][0]) == self.play.new_code:
            return {"status": live["status"], "fraction": live.get("userFraction")}
        done = self.play.track_state().completed
        new = done and int(done["versionCodes"][0]) == self.play.new_code
        return {"status": "completed" if new else "not_released", "fraction": 1.0 if new else None}


def load_scenario(name: str) -> dict:
    path = Path(name) if name.endswith(".yml") else SCENARIOS / f"{name}.yml"
    s = yaml.safe_load(path.read_text())
    s.setdefault("id", path.stem)
    return s


def run_scenario(scenario: dict, real_slack: dict | None = None, quiet: bool = True) -> Result:
    cfg = copy.deepcopy(config_mod.load(ROOT / "tests" / "release-bot.test.yml"))
    cfg["health"]["min_distinct_users"] = scenario.get("min_distinct_users", cfg["health"]["min_distinct_users"])

    clock = Clock(parse_time(scenario.get("submit_at", "thu 18:00")))
    play = FakePlay(clock, scenario["previous_release"], scenario["new_release"]["version_code"],
                    review_hours=scenario.get("review_hours", 8))
    slack = RecordingSlack(clock, cfg["slack"], set(scenario.get("on_duty", [])), real=real_slack)
    deps = cli.Deps(
        cfg=cfg, play=play, slack=slack,
        vitals=FakeVitals(clock, play, scenario, parse_time),
        crashlytics=FakeCrashlytics(clock, play, scenario, parse_time,
                                    cfg["health"]["crashlytics"]["new_issue_min_users"]),
        grafana=FakeGrafana(clock, scenario, parse_time),
    )
    result = Result(scenario, slack=slack, play=play)

    original_now = cli.utcnow
    cli.utcnow = lambda: clock.now
    try:
        for at, kind, payload in _timeline(scenario):
            clock.now = at
            _run_event(deps, result, kind, payload, quiet)
    finally:
        cli.utcnow = original_now

    _check_expectations(result)
    return result


def _timeline(s: dict) -> list[tuple[datetime, str, dict]]:
    submit_at = parse_time(s.get("submit_at", "thu 18:00"))
    end = parse_time(s.get("end_at", END))
    events = [(submit_at, "submit", {})]

    t = submit_at.replace(hour=0, minute=17)   # cron "17 */3 * * *"
    while t <= end:
        if t > submit_at:
            events.append((t, "check", {}))
        t += timedelta(hours=3)

    for day in ("mon", "tue", "wed"):
        at = parse_time(f"{day} 07:00")
        if at <= end:
            events.append((at, "advance", {}))

    for alert in s.get("grafana_alerts", []):
        if alert.get("webhook"):
            name = alert["labels"].get("alertname", "alert")
            events.append((parse_time(alert["from"]) + timedelta(minutes=2), "check",
                           {"trigger": f"Grafana alert {name}"}))

    for action in s.get("manual_actions", []):
        events.append((parse_time(action["at"]), action["command"], action))

    order = {"submit": 0, "halt": 1, "resume": 1, "advance": 2, "check": 3}
    return sorted(events, key=lambda e: (e[0], order[e[1]]))


def _cli(deps, argv: list[str], actor: str | None = None) -> tuple[int, str]:
    if actor:
        os.environ["GITHUB_ACTOR"] = actor
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = cli.main(argv, deps)
    except Exception as e:  # noqa: BLE001 — a failing step fails the simulated workflow run
        buf.write(f"::error::{e}\n")
        code = 1
    finally:
        os.environ.pop("GITHUB_ACTOR", None)
    return code, buf.getvalue().strip()


def _run_event(deps, result: Result, kind: str, payload: dict, quiet: bool) -> None:
    s = result.scenario
    at = deps.play.clock.now
    play = deps.play

    # Google approval happens between bot runs; log it when first observed.
    if play.approved_at and play.approved_at <= at and not getattr(play, "_approval_logged", False):
        play._approval_logged = True
        frac = play.fraction_at(play.approved_at) or 0
        result.runs.append(Run(play.approved_at, "Google Play", "review approved", True,
                               f"Google approved {s['new_release']['tag']} → rolling out to {frac:.0%} of users"))

    if kind == "submit":
        actor = s.get("submit_actor", "octocat")
        steps = [
            (["authorize", "--actor", actor], "Only the on-duty @android-release-hero may submit"),
            (["precheck-submit"] + (["--force"] if s.get("supersede") else []), "Previous rollout finished?"),
            (["submit", "--aab", "app-release.aab", "--version", s["new_release"]["tag"].lstrip("v"),
              "--release-url", f"https://github.com/ORG/REPO/releases/tag/{s['new_release']['tag']}"],
             "Submit to Play"),
        ]
        out, ok = [], True
        for argv, label in steps:
            code, text = _cli(deps, argv, actor)
            out.append(f"▸ {label}\n{text}" if text else f"▸ {label}")
            if code != 0:
                ok = False
                break
        result.runs.append(Run(at, "Android · Submit to Play", f"workflow_dispatch by {actor}", ok, "\n".join(out)))
    elif kind == "check":
        trigger = payload.get("trigger", "")
        code, text = _cli(deps, ["check", "--trigger", trigger] if trigger else ["check"])
        result.runs.append(Run(at, "Android · Health check",
                               "repository_dispatch (Grafana)" if trigger else "schedule", code == 0, text))
    elif kind == "advance":
        code, text = _cli(deps, ["advance"])
        result.runs.append(Run(at, "Android · Rollout step", "schedule", code == 0, text))
    elif kind in ("halt", "resume"):
        actor = payload.get("actor", "octocat")
        if kind == "resume":
            code, text = _cli(deps, ["authorize", "--actor", actor], actor)
            if code != 0:
                result.runs.append(Run(at, "Android · Resume rollout", f"workflow_dispatch by {actor}", False, text))
                return
        code, text = _cli(deps, [kind, "--reason", payload.get("reason", "")], actor)
        name = "Android · HALT rollout" if kind == "halt" else "Android · Resume rollout"
        result.runs.append(Run(at, name, f"workflow_dispatch by {actor}", code == 0, text))

    if not quiet:
        r = result.runs[-1]
        print(f"{r.at:%a %H:%M}  {'✅' if r.ok else '❌'} {r.workflow:<28} {r.trigger}")


def _check_expectations(result: Result) -> None:
    expect = result.scenario.get("expect", {})
    final = result.final
    if "final_status" in expect and final["status"] != expect["final_status"]:
        result.failures.append(f"final status {final['status']} != expected {expect['final_status']}")
    if "final_fraction" in expect and final["fraction"] != expect["final_fraction"]:
        result.failures.append(f"final fraction {final['fraction']} != expected {expect['final_fraction']}")
    if "submit_ok" in expect:
        submit = next((r for r in result.runs if r.workflow == "Android · Submit to Play"), None)
        if submit is None or submit.ok != expect["submit_ok"]:
            result.failures.append(f"submit ok={submit.ok if submit else None} != expected {expect['submit_ok']}")


def real_slack_from_env() -> dict | None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SIM_SLACK_CHANNEL")
    if not token or not channel:
        return None
    return {
        "token": token,
        "release": channel,
        "announce": os.environ.get("SIM_SLACK_ANNOUNCE_CHANNEL"),
        "alerts": os.environ.get("SIM_SLACK_ALERTS_CHANNEL"),
        "run_id": time.strftime("%H%M%S"),
    }
