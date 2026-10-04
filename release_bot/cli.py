"""Entry point: python -m release_bot <command>.

  status            print the production track
  precheck-submit   refuse to submit while last week's rollout is unfinished
  submit            upload the .aab and start the staged rollout
  check             health gate; halts on hard signals (cron every 3h + alerts)
  advance           health gate, then move one rollout step (cron Mon–Wed)
  halt / resume     manual controls
  authorize         is this GitHub user the on-duty @android-release-hero?
  vitals            print raw Play Vitals per versionCode (compare with Play Console)
  mock-inject       sandbox only: inject an incident into the mock health data
"""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from release_bot import config as config_mod
from release_bot import gate, schedule, tags
from release_bot.gate import Finding, Level, Verdict
from release_bot.play import Play, TrackState
from release_bot.slack import Slack


@dataclass
class Deps:
    """External clients, built lazily so tests can inject fakes."""
    cfg: dict
    play: Play
    slack: Slack
    vitals: object = None
    crashlytics: object = None
    grafana: object = None
    store: object = None                 # mock mode: state to persist after the command
    on_duty_logins: set | None = None    # mock mode: stand-in for @android-release-hero


def mock_mode() -> bool:
    return os.environ.get("RELEASE_BOT_MOCK", "").lower() == "true"


def build_mock_deps(cfg: dict, dry_run: bool) -> Deps:
    from release_bot import mock
    store = mock.MockStore()
    h = cfg["health"]
    return Deps(
        cfg=cfg,
        play=mock.MockPlay(store, dry_run),
        slack=Slack(os.environ.get("SLACK_BOT_TOKEN"), cfg["slack"]["channel_id"], dry_run=dry_run),
        vitals=mock.MockVitals(store) if h["play_vitals"]["enabled"] else None,
        crashlytics=mock.MockCrashlytics(store) if h["crashlytics"]["enabled"] else None,
        grafana=mock.MockGrafana(store) if h["grafana"]["enabled"] else None,
        store=store,
        on_duty_logins=mock.on_duty_logins(),
    )


def build_deps(cfg: dict, dry_run: bool, health: bool) -> Deps:
    if mock_mode():
        return build_mock_deps(cfg, dry_run)
    p = cfg["play"]
    deps = Deps(
        cfg=cfg,
        play=Play(cfg["package_name"], p["track"], p.get("changes_not_sent_for_review", False), dry_run),
        # Dry runs print Slack messages instead of posting them.
        slack=Slack(os.environ.get("SLACK_BOT_TOKEN"), cfg["slack"]["channel_id"], dry_run=dry_run),
    )
    if not health:
        return deps
    h = cfg["health"]
    if h["play_vitals"]["enabled"]:
        from release_bot.vitals import Vitals
        deps.vitals = Vitals(cfg["package_name"])
    if h["crashlytics"]["enabled"]:
        from release_bot.crashlytics import Crashlytics
        deps.crashlytics = Crashlytics(h["crashlytics"])
    if h["grafana"]["enabled"]:
        from release_bot.grafana import Grafana
        deps.grafana = Grafana(os.environ["GRAFANA_URL"], os.environ["GRAFANA_TOKEN"], h["grafana"]["matchers"])
    return deps


def collect_health(deps: Deps, live_code: int, prev_code: int | None) -> Verdict:
    """Ask every enabled source. A source that errors counts as HOLD: we can't
    prove the release is healthy, but an outage elsewhere shouldn't halt it."""
    h = deps.cfg["health"]
    verdict = Verdict()

    def run(source: str, fn):
        try:
            verdict.findings.extend(fn())
        except Exception as e:  # noqa: BLE001 — surface any source failure in Slack
            verdict.findings.append(Finding(source, Level.HOLD, f"could not fetch: {e}"))

    if deps.vitals:
        def vitals():
            by_version = deps.vitals.latest_by_version()
            return gate.evaluate_vitals(by_version.get(live_code), by_version.get(prev_code),
                                        h["play_vitals"], h["min_distinct_users"])
        run("play-vitals", vitals)
    if deps.crashlytics:
        run("crashlytics", lambda: gate.evaluate_crashlytics(
            deps.crashlytics.new_fatal_issues(live_code), h["crashlytics"]))
    if deps.grafana:
        run("grafana", lambda: gate.evaluate_grafana(deps.grafana.active_alerts(), h["grafana"]))
    return verdict


def utcnow() -> datetime:
    """Single clock for the bot, so the simulator can time-travel."""
    return datetime.now(timezone.utc)


def _mention(cfg: dict) -> str:
    group = cfg["slack"].get("hero_usergroup_id")
    return f"<!subteam^{group}> " if group else ""


def _alert(deps: Deps, text: str) -> None:
    deps.slack.announce(deps.cfg["slack"].get("alerts_channel_id"), text)


def _announce(deps: Deps, text: str) -> None:
    deps.slack.announce(deps.cfg["slack"].get("announce_channel_id"), text)


def _live_or_none(deps: Deps) -> tuple[TrackState, dict | None]:
    state = deps.play.track_state()
    return state, state.live


def cmd_status(deps: Deps, args) -> int:
    print(json.dumps(deps.play.track_state().releases, indent=2))
    return 0


def cmd_precheck_submit(deps: Deps, args) -> int:
    state, live = _live_or_none(deps)
    if live and live["status"] == "inProgress":
        pct = live.get("userFraction", 0) * 100
        msg = f"{live.get('name')} is still rolling out at {pct:.0f}%. A new release would stop it."
        if not args.force:
            print(f"::error::{msg} Re-run with 'supersede unfinished rollout' if that's intended.")
            return 1
        print(f"::warning::{msg} Continuing because --force was given.")
    prev = state.completed
    print(f"Previous fully released version: {prev.get('name') if prev else 'none'}")
    return 0


DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def plan_text(cfg: dict) -> str:
    """Human summary of the rollout plan, e.g. '20% Mon → 50% Tue → 100% Wed'."""
    targets = cfg["rollout"]["targets"]
    if len(targets) == 7 and len(set(targets.values())) == 1:
        return "one step per rollout run"
    by_day = sorted(targets.items(), key=lambda kv: DAYS.index(kv[0]))
    return " → ".join(f"{f:.0%} {day[:3].title()}" for day, f in by_day)


def cmd_submit(deps: Deps, args) -> int:
    p = deps.cfg["play"]
    notes = (args.notes or "").strip() or p["default_release_notes"]
    fraction = p["initial_fraction"]
    code = deps.play.upload_and_start(args.aab, args.version, fraction, notes, p["release_notes_language"])
    root = (f"🚀 Android *{args.version}* (versionCode {code}) submitted to Play review. "
            f"Rollout starts at {fraction:.0%} once Google approves.")
    if args.release_url:
        root += f"\nRelease notes: {args.release_url}"
    plan = plan_text(deps.cfg)
    deps.slack.post(args.version, f"Submitted. I'll keep checking health and step the rollout: {plan}.", root_text=root)
    _announce(deps, f"📦 Android *{args.version}* is in Play review. Staged rollout: "
                    f"{fraction:.0%} after approval → {plan}.")
    return 0


def cmd_check(deps: Deps, args) -> int:
    state, live = _live_or_none(deps)
    if not live or live["status"] != "inProgress":
        print("No in-progress rollout; nothing to check.")
        return 0
    version = live.get("name", "?")
    verdict = collect_health(deps, TrackState.version_code(live), TrackState.version_code(state.completed))
    print(verdict.scorecard())
    trigger = f"\nTriggered by: {args.trigger}" if args.trigger else ""

    if verdict.level == Level.HALT:
        deps.play.halt()
        pct = f"{live.get('userFraction', 0):.0%}"
        deps.slack.post(version, f"🛑 {_mention(deps.cfg)}*Rollout HALTED* at {pct}.{trigger}\n{verdict.scorecard()}\n"
                                 "Fix forward with a new build, or run *Android · Resume* if this was a false alarm.")
        _alert(deps, f"🛑 Android {version} rollout auto-halted at {pct}.{trigger}\n{verdict.scorecard()}")
        _announce(deps, f"🛑 Android *{version}* rollout halted at {pct} while we investigate.")
    elif verdict.level == Level.HOLD:
        # Runs every 3h: only speak up when the picture changed since the last post.
        if deps.slack.thread_contains(version, verdict.scorecard()):
            print("Same warning already posted; staying quiet.")
            return 0
        deps.slack.post(version, f"⚠️ Health needs a human look.{trigger}\n{verdict.scorecard()}")
        _alert(deps, f"⚠️ {_mention(deps.cfg)}Android {version} health needs a look.{trigger}\n{verdict.scorecard()}")
    return 0


def cmd_advance(deps: Deps, args) -> int:
    cfg = deps.cfg
    state, live = _live_or_none(deps)
    if not live:
        print("No staged rollout on the track; nothing to advance.")
        return 0
    version = live.get("name", "?")
    if live["status"] == "halted":
        deps.slack.post(version, "⏸ Still halted — not advancing. Resume manually when it's safe.")
        return 0

    current = live.get("userFraction", 1.0)
    now = utcnow()
    target = schedule.target_for(now, cfg["rollout"]["targets"], cfg["timezone"])
    nxt = schedule.next_fraction(current, cfg["rollout"]["steps"], target)
    if nxt is None:
        print(f"At {current:.0%}; today's target is {target}. Nothing to do.")
        return 0

    verdict = collect_health(deps, TrackState.version_code(live), TrackState.version_code(state.completed))
    print(verdict.scorecard())
    if verdict.level == Level.HALT:
        deps.play.halt()
        deps.slack.post(version, f"🛑 {_mention(cfg)}*Rollout HALTED* instead of moving to {nxt:.0%}.\n{verdict.scorecard()}")
        _alert(deps, f"🛑 Android {version} rollout auto-halted at {current:.0%}.\n{verdict.scorecard()}")
        _announce(deps, f"🛑 Android *{version}* rollout halted at {current:.0%} while we investigate.")
    elif verdict.level in (Level.HOLD, Level.NOT_ENOUGH_DATA):
        deps.slack.post(version, f"⏸ {_mention(cfg)}Holding at {current:.0%} (planned {nxt:.0%}).\n{verdict.scorecard()}")
    else:
        deps.play.set_fraction(nxt)
        label = "100% — fully released 🎉" if nxt >= 1.0 else f"{nxt:.0%}"
        deps.slack.post(version, f"⬆️ Rollout {current:.0%} → {label}\n{verdict.scorecard()}")
        if nxt >= 1.0:
            _announce(deps, f"🎉 Android *{version}* is fully released to 100% of users.")
    return 0


def cmd_halt(deps: Deps, args) -> int:
    release = deps.play.halt()
    who = os.environ.get("GITHUB_ACTOR", "someone")
    version = release.get("name", "?")
    deps.slack.post(version, f"🛑 {_mention(deps.cfg)}Halted manually by {who}. Reason: {args.reason or 'n/a'}")
    _alert(deps, f"🛑 Android {version} rollout halted manually by {who}. Reason: {args.reason or 'n/a'}")
    _announce(deps, f"🛑 Android *{version}* rollout halted while we investigate.")
    return 0


def cmd_resume(deps: Deps, args) -> int:
    release = deps.play.resume()
    who = os.environ.get("GITHUB_ACTOR", "someone")
    deps.slack.post(release.get("name", "?"),
                    f"▶️ Resumed by {who} at {release.get('userFraction', 0):.0%}. Reason: {args.reason or 'n/a'}")
    return 0


def cmd_authorize(deps: Deps, args) -> int:
    """Exit 0 only if the GitHub actor is mapped to a Slack user who is in
    @android-release-hero right now. Rotating the Slack group is all it takes
    to hand over the release."""
    if deps.on_duty_logins is not None:
        if args.actor not in deps.on_duty_logins:
            print(f"::error::{args.actor} is not the on-duty release hero (MOCK_ON_DUTY).")
            return 1
        print(f"{args.actor} is the on-duty release hero (mock).")
        return 0
    heroes = deps.cfg.get("access", {}).get("release_heroes", {}) or {}
    slack_id = heroes.get(args.actor)
    if not slack_id:
        print(f"::error::{args.actor} is not in access.release_heroes in release-bot.yml.")
        return 1
    group = deps.cfg["slack"]["hero_usergroup_id"]
    if slack_id not in deps.slack.usergroup_members(group):
        print(f"::error::{args.actor} is not the on-duty @android-release-hero this week.")
        return 1
    print(f"{args.actor} is the on-duty release hero.")
    return 0


def cmd_previous_tag(deps: Deps, args) -> int:
    out = subprocess.run(
        ["git", "tag", "--list", deps.cfg["tag_pattern"], "--sort=-v:refname"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    print(tags.previous_tag(args.tag, out) or "")
    return 0


def cmd_mock_inject(deps: Deps, args) -> int:
    from release_bot import mock
    if deps.store is None:
        print("::error::mock-inject only works with RELEASE_BOT_MOCK=true")
        return 1
    deps.store.data["incident"] = args.incident
    live = deps.play.track_state().live
    print(f"Mock incident set to '{args.incident}': {json.dumps(mock.INCIDENTS[args.incident])}")
    if live:
        who = os.environ.get("GITHUB_ACTOR", "someone")
        text = ("🧪 Mock incidents cleared" if args.incident == "none"
                else f"🧪 Mock incident injected by {who}: `{args.incident}`. Health check runs next.")
        deps.slack.post(live.get("name", "?"), text)
    return 0


def cmd_vitals(deps: Deps, args) -> int:
    print(json.dumps(deps.vitals.latest_by_version(), indent=2, sort_keys=True))
    return 0


COMMANDS = {
    "status": cmd_status,
    "precheck-submit": cmd_precheck_submit,
    "submit": cmd_submit,
    "check": cmd_check,
    "advance": cmd_advance,
    "halt": cmd_halt,
    "resume": cmd_resume,
    "authorize": cmd_authorize,
    "vitals": cmd_vitals,
    "previous-tag": cmd_previous_tag,
    "mock-inject": cmd_mock_inject,
}

NEEDS_HEALTH = {"check", "advance", "vitals"}


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="release_bot")
    ap.add_argument("--config", default=str(config_mod.DEFAULT_PATH))
    ap.add_argument("--dry-run", action="store_true", help="read everything, change nothing on Play")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("precheck-submit").add_argument("--force", action="store_true")
    s = sub.add_parser("submit")
    s.add_argument("--aab", required=True)
    s.add_argument("--version", required=True)
    s.add_argument("--notes", default="")
    s.add_argument("--release-url", default="")
    sub.add_parser("check").add_argument("--trigger", default="")
    sub.add_parser("advance")
    sub.add_parser("halt").add_argument("--reason", default="")
    sub.add_parser("resume").add_argument("--reason", default="")
    sub.add_parser("authorize").add_argument("--actor", required=True)
    sub.add_parser("vitals")
    sub.add_parser("previous-tag").add_argument("--tag", required=True)
    from release_bot.mock import INCIDENTS
    sub.add_parser("mock-inject").add_argument("--incident", required=True, choices=sorted(INCIDENTS))
    return ap.parse_args(argv)


def main(argv=None, deps: Deps | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if deps is None:
        deps = build_deps(config_mod.load(args.config), args.dry_run, args.command in NEEDS_HEALTH)
    try:
        return COMMANDS[args.command](deps, args)
    finally:
        if deps.store is not None and not args.dry_run:
            deps.store.save()
