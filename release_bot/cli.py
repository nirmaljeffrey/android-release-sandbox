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
  apps              JSON list of configured apps (for workflow matrices)
  app-info          resolve one app + tag into workflow outputs
  plan              each app's rollout timeline (Android/iOS) and health rules
  validate          fail if any schedule or health rule is invalid (CI)

Every command takes --app <id> when release-bot.yml defines several apps.
"""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from release_bot import config as config_mod
from release_bot import gate, policy, rules, schedule, tags
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
    store = mock.MockStore(app_id=cfg.get("app_id", "app"))
    cfg = {**cfg, "_mock": True}
    return Deps(
        cfg=cfg,
        play=mock.MockPlay(store, dry_run),
        slack=Slack(os.environ.get("SLACK_BOT_TOKEN"), cfg["slack"]["channel_id"], dry_run=dry_run),
        # The demo always shows every source, whatever release-bot.yml enables.
        vitals=mock.MockVitals(store),
        crashlytics=mock.MockCrashlytics(store),
        grafana=mock.MockGrafana(store),
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
    norm = rules.normalize(cfg.get("health"))
    sources = norm["sources"]
    if "play_vitals" in sources:
        from release_bot.vitals import Vitals
        deps.vitals = Vitals(cfg["package_name"])
    if "crashlytics" in sources:
        from release_bot.crashlytics import Crashlytics
        deps.crashlytics = Crashlytics({"new_issue_min_users": rules.crashlytics_query_min_users(norm),
                                        **sources["crashlytics"]})
    if "grafana" in sources:
        from release_bot.grafana import Grafana
        deps.grafana = Grafana(os.environ["GRAFANA_URL"], os.environ["GRAFANA_TOKEN"],
                               sources["grafana"].get("matchers", []))
    return deps


def collect_health(deps: Deps, live_code: int, prev_code: int | None) -> Verdict:
    """Query each configured source, then apply the health rules. A source that
    errors counts as HOLD: we can't prove the release is healthy, but an outage
    elsewhere shouldn't halt it."""
    norm = rules.normalize(deps.cfg.get("health"))
    wanted = set(norm["sources"])
    if deps.store is not None:
        wanted = set(rules.SOURCES)  # mock mode: the demo exercises every source
    signals: dict = {}

    def fetch(source: str, fn):
        try:
            signals[source] = fn()
        except Exception as e:  # noqa: BLE001 — surface any source failure in Slack
            signals[source] = e

    if deps.vitals and "play_vitals" in wanted:
        def vitals():
            by_version = deps.vitals.latest_by_version()
            return {"new": by_version.get(live_code), "prev": by_version.get(prev_code)}
        fetch("play_vitals", vitals)
    if deps.crashlytics and "crashlytics" in wanted:
        fetch("crashlytics", lambda: {"new_issues": deps.crashlytics.new_fatal_issues(live_code)})
    if deps.grafana and "grafana" in wanted:
        fetch("grafana", lambda: {"alerts": deps.grafana.active_alerts()})
    return Verdict(rules.evaluate(norm, signals))


def utcnow() -> datetime:
    """Single clock for the bot, so the simulator can time-travel."""
    return datetime.now(timezone.utc)


def _name(deps: Deps) -> str:
    """'Android' for a single-app setup, 'Shop Android' when several apps share the bot."""
    return deps.cfg.get("display_name", "Android")


def _key(deps: Deps, version: str) -> str:
    """Slack thread key: the version, prefixed by the app id in multi-app setups."""
    app_id = deps.cfg.get("app_id", config_mod.LEGACY_APP_ID)
    return version if app_id == config_mod.LEGACY_APP_ID else f"{app_id} {version}"


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


def _p(fraction: float) -> str:
    return f"{fraction * 100:g}%"


def rollout_policy(cfg: dict) -> policy.RolloutPolicy:
    return policy.from_config(cfg)


def plan_text(cfg: dict) -> str:
    """Human summary of the rollout plan, e.g. '1% Mon → 2% Tue → 100% Wed'."""
    if cfg.get("_mock"):
        return "one step per rollout run"
    return rollout_policy(cfg).android.summary()


def cmd_submit(deps: Deps, args) -> int:
    p = deps.cfg["play"]
    notes = (args.notes or "").strip() or p["default_release_notes"]
    fraction = rollout_policy(deps.cfg).android.initial
    code = deps.play.upload_and_start(args.aab, args.version, fraction, notes, p["release_notes_language"])
    root = (f"🚀 {_name(deps)} *{args.version}* (versionCode {code}) submitted to Play review. "
            f"Rollout starts at {_p(fraction)} once Google approves.")
    if args.release_url:
        root += f"\nRelease notes: {args.release_url}"
    plan = plan_text(deps.cfg)
    deps.slack.post(_key(deps, args.version), f"Submitted. I'll keep checking health and step the rollout: {plan}.", root_text=root)
    _announce(deps, f"📦 {_name(deps)} *{args.version}* is in Play review. Staged rollout: "
                    f"{plan}.")
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
    key = _key(deps, version)

    if verdict.level == Level.HALT:
        deps.play.halt()
        pct = _p(live.get("userFraction", 0))
        deps.slack.post(key, f"🛑 {_mention(deps.cfg)}*Rollout HALTED* at {pct}.{trigger}\n{verdict.scorecard()}\n"
                             "Fix forward with a new build, or run *Android · Resume* if this was a false alarm.")
        _alert(deps, f"🛑 {_name(deps)} {version} rollout auto-halted at {pct}.{trigger}\n{verdict.scorecard()}")
        _announce(deps, f"🛑 {_name(deps)} *{version}* rollout halted at {pct} while we investigate.")
    elif verdict.level in (Level.HOLD, Level.NOTIFY):
        # Runs every few hours: only speak up when the picture changed since the last post.
        if deps.slack.thread_contains(key, verdict.scorecard()):
            print("Same message already posted; staying quiet.")
            return 0
        if verdict.level == Level.HOLD:
            deps.slack.post(key, f"⚠️ Health needs a human look.{trigger}\n{verdict.scorecard()}")
            _alert(deps, f"⚠️ {_mention(deps.cfg)}{_name(deps)} {version} health needs a look.{trigger}\n{verdict.scorecard()}")
        else:
            deps.slack.post(key, f"🔔 FYI, still rolling out.{trigger}\n{verdict.scorecard()}")
            _alert(deps, f"🔔 {_name(deps)} {version}: FYI, still rolling out.{trigger}\n{verdict.scorecard()}")
    return 0


def _submitted_at(deps: Deps, version: str, code: int | None) -> datetime | None:
    """When this release was submitted, for 'day N' schedules. Stateless: mock
    history, or the GitHub release the submit workflow created for the tag."""
    if deps.store is not None:
        return deps.store.submitted_at(code)
    tag = f"{deps.cfg.get('tag_prefix', '')}v{version}"
    repo = deps.cfg.get("repository") or os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not repo:
        return None
    import requests
    resp = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", timeout=30,
                        headers={"Authorization": f"Bearer {token}"} if token else {})
    if resp.status_code != 200:
        print(f"::warning::Couldn't read release {tag} in {repo} ({resp.status_code}); day-based steps wait.")
        return None
    return datetime.fromisoformat(resp.json()["created_at"].replace("Z", "+00:00"))


def cmd_advance(deps: Deps, args) -> int:
    cfg = deps.cfg
    state, live = _live_or_none(deps)
    if not live:
        print("No staged rollout on the track; nothing to advance.")
        return 0
    version = live.get("name", "?")
    key = _key(deps, version)
    if live["status"] == "halted":
        deps.slack.post(key, "⏸ Still halted — not advancing. Resume manually when it's safe.")
        return 0

    plan = rollout_policy(cfg)
    sched = plan.android
    current = live.get("userFraction", 1.0)
    submitted = _submitted_at(deps, version, TrackState.version_code(live)) if sched.kind == "day" else None
    target = sched.target(utcnow(), cfg["timezone"], submitted)
    if deps.store is not None:
        target = 1.0  # mock mode: every "Rollout step" run moves one step, any day of the week
    nxt = sched.next_fraction(current, target)
    if nxt is None:
        print(f"At {_p(current)}; nothing due today (schedule: {sched.summary()}).")
        return 0

    verdict = collect_health(deps, TrackState.version_code(live), TrackState.version_code(state.completed))
    print(verdict.scorecard())
    thin_blocks = verdict.level == Level.NOT_ENOUGH_DATA and plan.when_data_is_thin == "hold"
    if verdict.level == Level.HALT:
        deps.play.halt()
        deps.slack.post(key, f"🛑 {_mention(cfg)}*Rollout HALTED* instead of moving to {_p(nxt)}.\n{verdict.scorecard()}")
        _alert(deps, f"🛑 {_name(deps)} {version} rollout auto-halted at {_p(current)}.\n{verdict.scorecard()}")
        _announce(deps, f"🛑 {_name(deps)} *{version}* rollout halted at {_p(current)} while we investigate.")
    elif verdict.level == Level.HOLD or thin_blocks:
        deps.slack.post(key, f"⏸ {_mention(cfg)}Holding at {_p(current)} (planned {_p(nxt)}).\n{verdict.scorecard()}")
    else:
        deps.play.set_fraction(nxt)
        label = "100% — fully released 🎉" if nxt >= 1.0 else _p(nxt)
        deps.slack.post(key, f"⬆️ Rollout {_p(current)} → {label}\n{verdict.scorecard()}")
        if verdict.level == Level.NOTIFY:
            _alert(deps, f"🔔 {_name(deps)} {version} moved to {_p(nxt)}; FYI:\n{verdict.scorecard()}")
        if nxt >= 1.0:
            _announce(deps, f"🎉 {_name(deps)} *{version}* is fully released to 100% of users.")
    return 0


def cmd_halt(deps: Deps, args) -> int:
    release = deps.play.halt()
    who = os.environ.get("GITHUB_ACTOR", "someone")
    version = release.get("name", "?")
    deps.slack.post(_key(deps, version), f"🛑 {_mention(deps.cfg)}Halted manually by {who}. Reason: {args.reason or 'n/a'}")
    _alert(deps, f"🛑 {_name(deps)} {version} rollout halted manually by {who}. Reason: {args.reason or 'n/a'}")
    _announce(deps, f"🛑 {_name(deps)} *{version}* rollout halted while we investigate.")
    return 0


def cmd_resume(deps: Deps, args) -> int:
    release = deps.play.resume()
    who = os.environ.get("GITHUB_ACTOR", "someone")
    deps.slack.post(_key(deps, release.get("name", "?")),
                    f"▶️ Resumed by {who} at {_p(release.get('userFraction', 0))}. Reason: {args.reason or 'n/a'}")
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
        deps.slack.post(_key(deps, live.get("name", "?")), text)
    return 0


def cmd_vitals(deps: Deps, args) -> int:
    print(json.dumps(deps.vitals.latest_by_version(), indent=2, sort_keys=True))
    return 0


def cmd_app_info(deps: Deps, args) -> int:
    """Workflow outputs for one app + tag (`key=value` lines for $GITHUB_OUTPUT)."""
    cfg = deps.cfg
    try:
        version = config_mod.version_from_tag(cfg, args.tag) if args.tag else ""
    except config_mod.ConfigError as e:
        print(f"::error::{e}")
        return 1
    out = {
        "app": cfg["app_id"],
        "name": cfg["display_name"],
        "environment": cfg["environment"],
        "signing_environment": cfg["signing_environment"],
        "repository": cfg["repository"],
        "package_name": cfg["package_name"],
        "version": version,
        "tag_pattern": cfg["tag_pattern"],
        "project_dir": cfg["build"]["project_dir"],
        "bundle_task": cfg["build"]["bundle_task"],
        "aab_glob": cfg["build"]["aab_glob"],
        "java_version": str(cfg["build"]["java_version"]),
    }
    for k, v in out.items():
        print(f"{k}={v}")
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
    "app-info": cmd_app_info,
}

NEEDS_HEALTH = {"check", "advance", "vitals"}


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="release_bot")
    ap.add_argument("--config", default=str(config_mod.DEFAULT_PATH))
    ap.add_argument("--dry-run", action="store_true", help="read everything, change nothing on Play")
    ap.add_argument("--app", default=None, help="app id from release-bot.yml (needed when several apps are configured)")
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
    sub.add_parser("apps")
    sub.add_parser("plan")
    sub.add_parser("validate")
    sub.add_parser("app-info").add_argument("--tag", default="")
    return ap.parse_args(argv)


def _check_app(cfg: dict) -> tuple[list[str], list[str], list[dict]]:
    """(errors, warnings, plan rows) for one resolved app."""
    platforms = cfg.get("platforms", ["android"])
    try:
        rules.normalize(cfg.get("health"))
        pol = rollout_policy(cfg)
    except (policy.PolicyError, rules.RuleError) as e:
        return [str(e)], [], []
    errors, warnings = policy.check(pol, platforms)
    return errors, warnings, policy.plan_rows(pol, platforms)


def cmd_plan(raw: dict, app: str | None) -> int:
    """Print each app's rollout timeline per platform, with any problems."""
    ids = [app] if app else config_mod.app_ids(raw)
    failed = False
    for app_id in ids:
        cfg = config_mod.resolve(raw, app_id)
        errors, warnings, rows = _check_app(cfg)
        norm = rules.normalize(cfg.get("health")) if not errors else {"rules": [], "sources": {}, "min_users": 0}
        print(f"\n{cfg['display_name']} ({app_id}) · account {cfg['account']} · {cfg['environment']}")
        for r in rows:
            print(f"  {r['platform']:<8} {r['when']:<12} {r['percent']:>5}   {r['how']}")
        if norm["rules"]:
            print(f"  Health (min {norm['min_users']} users; sources: {', '.join(norm['sources']) or 'none'}):")
            for rule in norm["rules"]:
                cond = (f"≥ {rule.above * 100:g}%" if rule.above is not None else
                        f"> previous +{rule.above_previous_by * 100:g}%" if rule.above_previous_by is not None else
                        f"≥ {rule.at_least} users" if rule.at_least is not None else
                        f"severity {'/'.join(rule.severity)}" + (f", alert ~ /{rule.alert}/" if rule.alert else ""))
                off = "" if rule.source in norm["sources"] else "  [source off]"
                print(f"    {rule.action:<6} {rule.source}:{rule.metric or 'alert'} {cond}  ({rule.name}){off}")
        for w in warnings:
            print(f"  ⚠️  {w}")
        for e in errors:
            print(f"  ❌ {e}")
        failed |= bool(errors)
    return 1 if failed else 0


def cmd_validate(raw: dict, app: str | None) -> int:
    """Exit non-zero if any app's schedule or health rules are invalid."""
    ids = [app] if app else config_mod.app_ids(raw)
    failed = False
    for app_id in ids:
        try:
            cfg = config_mod.resolve(raw, app_id)
        except config_mod.ConfigError as e:
            print(f"::error::{e}")
            failed = True
            continue
        errors, warnings, _ = _check_app(cfg)
        for w in warnings:
            print(f"::warning::{app_id}: {w}")
        for e in errors:
            print(f"::error::{app_id}: {e}")
        failed |= bool(errors)
    print("release-bot.yml is invalid" if failed else f"release-bot.yml OK ({len(ids)} app(s))")
    return 1 if failed else 0


def cmd_apps(raw: dict) -> int:
    """JSON matrix of every app: [{"app", "environment", "account"}]."""
    rows = []
    for app_id in config_mod.app_ids(raw):
        cfg = config_mod.resolve(raw, app_id)
        rows.append({"app": app_id, "environment": cfg["environment"], "account": cfg["account"]})
    print(json.dumps(rows))
    return 0


def main(argv=None, deps: Deps | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if deps is None:
        raw = config_mod.load(args.config)
        if args.command == "apps":
            return cmd_apps(raw)
        if args.command in ("plan", "validate"):
            return (cmd_plan if args.command == "plan" else cmd_validate)(raw, args.app)
        try:
            cfg = config_mod.resolve(raw, args.app)
        except config_mod.ConfigError as e:
            print(f"::error::{e}")
            return 2
        deps = build_deps(cfg, args.dry_run, args.command in NEEDS_HEALTH)
    try:
        return COMMANDS[args.command](deps, args)
    finally:
        if deps.store is not None and not args.dry_run:
            deps.store.save()
